"""REST endpoints for the audit agent."""

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import AuditResult, Feed, PipelineVersion, create_pipeline_version, get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feeds", tags=["audits"])


class TriggerAuditRequest(BaseModel):
    start: datetime
    end: datetime
    enable_replay: bool = True
    enable_discovery: bool = True


class ApplyAuditRequest(BaseModel):
    save: bool = True
    force: bool = False  # if True, re-run the remediation agent even if already cached


@router.get("/{feed_id}/audits")
def list_audits(feed_id: str, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """List all audit results for a feed, newest first."""
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    records = (
        db.query(AuditResult)
        .filter(AuditResult.feed_id == feed_id)
        .order_by(AuditResult.created_at.desc())
        .all()
    )
    return [_summarize_record(r, db) for r in records]


@router.get("/{feed_id}/audits/{audit_id}")
def get_audit(feed_id: str, audit_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Get the full audit report for a specific audit run."""
    record = db.get(AuditResult, audit_id)
    if record is None or record.feed_id != feed_id:
        raise HTTPException(status_code=404, detail="Audit not found")

    result = _summarize_record(record, db)
    if record.result_json:
        try:
            result["report"] = json.loads(record.result_json)
        except Exception:
            result["report"] = None
    if record.proposed_config_json:
        try:
            result["proposed_config"] = json.loads(record.proposed_config_json)
        except Exception:
            result["proposed_config"] = None
    return result


@router.post("/{feed_id}/audits", status_code=202)
async def trigger_audit(
    feed_id: str,
    req: TriggerAuditRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Manually trigger an audit for a feed. Runs asynchronously in the background."""
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")
    if feed.status != "ready":
        raise HTTPException(status_code=400, detail=f"Feed is not ready (status={feed.status})")
    if not feed.config_json:
        raise HTTPException(status_code=400, detail="Feed has no pipeline config")

    if req.end <= req.start:
        raise HTTPException(status_code=422, detail="end must be after start")

    from app.agents.audit_agent.orchestrator import run_and_persist_audit

    background_tasks.add_task(
        _run_audit_task,
        feed_id=feed_id,
        start=req.start,
        end=req.end,
        enable_replay=req.enable_replay,
        enable_discovery=req.enable_discovery,
    )

    return {
        "status": "accepted",
        "feed_id": feed_id,
        "period_start": req.start.isoformat(),
        "period_end": req.end.isoformat(),
        "message": "Audit started. Poll GET /{feed_id}/audits for results.",
    }


@router.delete("/{feed_id}/audits/{audit_id}", status_code=204)
def delete_audit(feed_id: str, audit_id: str, db: Session = Depends(get_db)) -> None:
    """Delete an audit result."""
    record = db.get(AuditResult, audit_id)
    if record is None or record.feed_id != feed_id:
        raise HTTPException(status_code=404, detail="Audit not found")
    db.delete(record)
    db.commit()


@router.post("/{feed_id}/audits/{audit_id}/apply")
async def apply_audit(
    feed_id: str,
    audit_id: str,
    req: ApplyAuditRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from app.agents.pipeline_agent.runtime import DEFAULT_AGENT_MODEL, run_audit_remediation_agent

    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")
    if not feed.config_json:
        raise HTTPException(status_code=400, detail="Feed has no pipeline config")

    record = db.get(AuditResult, audit_id)
    if record is None or record.feed_id != feed_id:
        raise HTTPException(status_code=404, detail="Audit not found")
    if record.status != "complete":
        raise HTTPException(status_code=400, detail=f"Audit is not complete (status={record.status})")
    if not record.result_json:
        raise HTTPException(status_code=400, detail="Audit report is missing")

    try:
        audit_report = json.loads(record.result_json)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Audit report JSON is invalid") from exc

    try:
        config = json.loads(feed.config_json)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Feed config JSON is invalid") from exc

    # Reuse already-generated proposed config unless caller requests a fresh run
    proposed_config = None
    if record.proposed_config_json and not req.force:
        try:
            proposed_config = json.loads(record.proposed_config_json)
            summary = proposed_config.get("_summary", "")
        except Exception:
            proposed_config = None

    if proposed_config is None:
        topic = str(config.get("topic") or feed.topic or "").strip()
        current_sources = config.get("sources", [])
        current_blocks = config.get("blocks", [])

        try:
            remediation = await run_audit_remediation_agent(
                topic,
                current_sources,
                current_blocks,
                audit_report,
                model=DEFAULT_AGENT_MODEL,
                verbose=True,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        summary = remediation["summary"]
        proposed_config = {
            "sources": remediation["sources"],
            "blocks": remediation["blocks_json"],
            "_summary": summary,
        }

        # Persist so subsequent calls (and page refreshes) don't re-run the agent
        record.proposed_config_json = json.dumps(proposed_config)
        db.commit()

    if req.save:
        saveable = {k: v for k, v in proposed_config.items() if not k.startswith("_")}
        new_config_json = json.dumps({**config, **saveable})
        feed.config_json = new_config_json
        create_pipeline_version(
            feed_id, new_config_json, db, label=f"Applied audit #{audit_id[:8]}"
        )
        db.commit()
        db.refresh(feed)

    return {
        "feed": {
            "id": feed.id,
            "name": feed.name,
            "topic": feed.topic,
            "status": feed.status,
            "poll_interval_hours": feed.poll_interval_hours,
            "created_at": feed.created_at.isoformat() if feed.created_at else None,
            "last_polled_at": feed.last_polled_at.isoformat() if feed.last_polled_at else None,
            "error_message": feed.error_message,
            "config": json.loads(feed.config_json) if feed.config_json else None,
        },
        "saved": req.save,
        "summary": summary,
        "proposed_config": proposed_config,
    }


async def _run_audit_task(
    *,
    feed_id: str,
    start: datetime,
    end: datetime,
    enable_replay: bool,
    enable_discovery: bool,
) -> None:
    """Background task: dispatch audit job to the worker service."""
    from app.worker.client import dispatch_audit

    try:
        await dispatch_audit(
            feed_id=feed_id,
            start=start,
            end=end,
            enable_replay=enable_replay,
            enable_discovery=enable_discovery,
        )
    except Exception:
        logger.exception("Failed to dispatch audit feed_id=%s", feed_id)


def _summarize_record(record: AuditResult, db=None) -> dict[str, Any]:
    version_number = None
    if record.pipeline_version_id and db is not None:
        version = db.get(PipelineVersion, record.pipeline_version_id)
        if version:
            version_number = version.version_number
    return {
        "id": record.id,
        "feed_id": record.feed_id,
        "status": record.status,
        "audit_period_start": record.audit_period_start.isoformat() if record.audit_period_start else None,
        "audit_period_end": record.audit_period_end.isoformat() if record.audit_period_end else None,
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "error_message": record.error_message,
        "pipeline_version_id": record.pipeline_version_id,
        "pipeline_version_number": version_number,
    }
