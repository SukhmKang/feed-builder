"""End-to-end orchestration for the audit agent."""

import json
import logging
from datetime import datetime, timezone

from app.database import AuditResult, Feed, PipelineVersion, SessionLocal
from app.agents.pipeline_agent.runtime import DEFAULT_AGENT_MODEL

from .audit_critic import run_audit_critic
from .data_collector import collect_audit_data
from .summarizer import build_audit_summary_payload
from .types import AuditReport

logger = logging.getLogger(__name__)


async def run_audit(
    feed_id: str,
    *,
    start: datetime,
    end: datetime,
    model: str = DEFAULT_AGENT_MODEL,
    enable_replay: bool = True,
    enable_discovery: bool = True,
    db=None,
) -> AuditReport:
    """Run the full audit flow for a feed over the given period.

    Does NOT write to DB — the caller handles persistence.
    """
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        return await _run(
            feed_id,
            start=start,
            end=end,
            model=model,
            enable_replay=enable_replay,
            enable_discovery=enable_discovery,
            db=db,
        )
    finally:
        if own_db:
            db.close()


async def _run(
    feed_id: str,
    *,
    start: datetime,
    end: datetime,
    model: str,
    enable_replay: bool,
    enable_discovery: bool,
    db,
) -> AuditReport:
    logger.info(
        "audit.start feed_id=%s period=%s→%s enable_replay=%s enable_discovery=%s",
        feed_id, start.isoformat(), end.isoformat(), enable_replay, enable_discovery,
    )

    # Load feed config
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise ValueError(f"Feed not found: {feed_id}")
    if not feed.config_json:
        raise ValueError(f"Feed {feed_id} has no config_json")

    config = json.loads(feed.config_json)
    topic = str(config.get("topic") or feed.topic or "").strip()
    sources = config.get("sources", [])
    blocks_json = config.get("blocks", [])

    # Collect data
    stats, passed, filtered = await collect_audit_data(
        feed_id, start, end, db=db, enable_replay=enable_replay
    )
    logger.info(
        "audit.data_collected passed=%s filtered=%s",
        len(passed), len(filtered),
    )

    # Build stratified summary payload
    payload = build_audit_summary_payload(stats, passed, filtered)

    # Run the four-step critique
    assessment, manual_override_assessment, pipeline_recs, source_recs, proposed_new_sources = await run_audit_critic(
        topic=topic,
        payload=payload,
        blocks_json=blocks_json,
        current_sources=sources,
        model=model,
        enable_discovery=enable_discovery,
    )

    now = datetime.now(tz=timezone.utc)
    report = AuditReport(
        feed_id=feed_id,
        topic=topic,
        audit_period_start=start.isoformat(),
        audit_period_end=end.isoformat(),
        stats=stats,
        assessment=assessment,
        manual_override_assessment=manual_override_assessment,
        pipeline_recommendations=pipeline_recs,
        source_recommendations=source_recs,
        proposed_new_sources=proposed_new_sources,
        current_config_snapshot={"sources": sources, "blocks": blocks_json},
        generated_at=now.isoformat(),
    )
    logger.info("audit.complete feed_id=%s", feed_id)
    return report


async def run_and_persist_audit(
    feed_id: str,
    *,
    start: datetime,
    end: datetime,
    model: str = DEFAULT_AGENT_MODEL,
    enable_replay: bool = True,
    enable_discovery: bool = True,
) -> str:
    """Wrap run_audit() with DB persistence. Returns the AuditResult.id."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        active_version = db.query(PipelineVersion).filter(
            PipelineVersion.feed_id == feed_id,
            PipelineVersion.is_active.is_(True),
        ).first()
        record = AuditResult(
            feed_id=feed_id,
            audit_period_start=start,
            audit_period_end=end,
            status="running",
            started_at=now,
            pipeline_version_id=active_version.id if active_version else None,
        )
        db.add(record)
        db.commit()
        audit_id = record.id
        logger.info("audit.persisted_start audit_id=%s feed_id=%s", audit_id, feed_id)

        try:
            report = await run_audit(
                feed_id,
                start=start,
                end=end,
                model=model,
                enable_replay=enable_replay,
                enable_discovery=enable_discovery,
                db=db,
            )
            record.result_json = json.dumps(report)
            record.status = "complete"
            record.completed_at = datetime.utcnow()
            db.commit()
            logger.info("audit.persisted_complete audit_id=%s", audit_id)

            # Generate and persist the proposed pipeline config
            try:
                from app.agents.pipeline_agent.runtime import run_audit_remediation_agent
                snapshot = report.get("current_config_snapshot", {})
                remediation = await run_audit_remediation_agent(
                    str(snapshot.get("topic") or report.get("topic") or "").strip(),
                    snapshot.get("sources", []),
                    snapshot.get("blocks", []),
                    report,
                    model=model,
                    verbose=False,
                )
                proposed_config = {
                    "sources": remediation["sources"],
                    "blocks": remediation["blocks_json"],
                    "_summary": remediation["summary"],
                }
                record.proposed_config_json = json.dumps(proposed_config)
                db.commit()
                logger.info("audit.proposed_config_persisted audit_id=%s", audit_id)
            except Exception:
                logger.exception("audit.proposed_config_failed audit_id=%s — continuing", audit_id)
        except Exception as exc:
            logger.exception("audit.failed audit_id=%s feed_id=%s", audit_id, feed_id)
            record.status = "error"
            record.error_message = str(exc)
            record.completed_at = datetime.utcnow()
            db.commit()
            raise

        return audit_id
    finally:
        db.close()


__all__ = ["run_audit", "run_and_persist_audit"]
