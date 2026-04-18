"""Feed report endpoints — generate, list, download, delete."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import Feed, FeedReport, get_db
from app.services import r2_storage
from app.services.report_generator import build_pdf, query_stories_for_report

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feeds/{feed_id}/reports", tags=["reports"])


# ── Serialiser ────────────────────────────────────────────────────────────────

def _serialise(report: FeedReport) -> dict[str, Any]:
    return {
        "id": report.id,
        "feed_id": report.feed_id,
        "date_from": report.date_from.isoformat() if report.date_from else None,
        "date_to": report.date_to.isoformat() if report.date_to else None,
        "story_count": report.story_count,
        "r2_key": report.r2_key,
        "created_at": report.created_at.isoformat() if report.created_at else None,
    }


# ── Request body ──────────────────────────────────────────────────────────────

class GenerateReportRequest(BaseModel):
    date_from: str   # ISO-8601 date string
    date_to: str     # ISO-8601 date string


def _parse_date(value: str, field: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field} is not a valid ISO-8601 date")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("")
def generate_report(
    feed_id: str,
    body: GenerateReportRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    date_from = _parse_date(body.date_from, "date_from")
    date_to = _parse_date(body.date_to, "date_to")

    if date_from >= date_to:
        raise HTTPException(status_code=422, detail="date_from must be before date_to")

    # Set date_to to end-of-day so the range is inclusive
    date_to = date_to.replace(hour=23, minute=59, second=59, microsecond=999999)

    stories = query_stories_for_report(db, feed_id, date_from, date_to)

    pdf_bytes = build_pdf(
        feed_name=feed.name or "Feed",
        feed_topic=feed.topic or "",
        date_from=date_from,
        date_to=date_to,
        stories=stories,
    )

    report_id = str(uuid.uuid4())
    r2_key = f"reports/{feed_id}/{report_id}.pdf"

    try:
        r2_storage.upload_pdf(r2_key, pdf_bytes)
    except Exception as exc:
        logger.error("generate_report: R2 upload failed for feed %s: %s", feed_id, exc)
        raise HTTPException(status_code=500, detail=f"Failed to upload report to storage: {exc}") from exc

    record = FeedReport(
        id=report_id,
        feed_id=feed_id,
        date_from=date_from,
        date_to=date_to,
        story_count=len(stories),
        r2_key=r2_key,
        created_at=datetime.now(timezone.utc),
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    logger.info(
        "generate_report: created report %s for feed %s (%d stories)",
        report_id, feed_id, len(stories),
    )
    return _serialise(record)


@router.get("")
def list_reports(
    feed_id: str,
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    records = (
        db.query(FeedReport)
        .filter(FeedReport.feed_id == feed_id)
        .order_by(FeedReport.created_at.desc())
        .all()
    )
    return [_serialise(r) for r in records]


@router.get("/{report_id}/download")
def download_report(
    feed_id: str,
    report_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    record = db.get(FeedReport, report_id)
    if record is None or record.feed_id != feed_id:
        raise HTTPException(status_code=404, detail="Report not found")
    if not record.r2_key:
        raise HTTPException(status_code=404, detail="Report file not available")

    try:
        url = r2_storage.presigned_download_url(record.r2_key)
    except Exception as exc:
        logger.error("download_report: presigned URL failed for %s: %s", report_id, exc)
        raise HTTPException(status_code=500, detail="Could not generate download URL") from exc

    return RedirectResponse(url=url, status_code=302)


@router.delete("/{report_id}", status_code=204)
def delete_report(
    feed_id: str,
    report_id: str,
    db: Session = Depends(get_db),
) -> None:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    record = db.get(FeedReport, report_id)
    if record is None or record.feed_id != feed_id:
        raise HTTPException(status_code=404, detail="Report not found")

    if record.r2_key:
        try:
            r2_storage.delete_object(record.r2_key)
        except Exception as exc:
            logger.warning("delete_report: R2 delete failed for %s: %s", report_id, exc)

    db.delete(record)
    db.commit()
