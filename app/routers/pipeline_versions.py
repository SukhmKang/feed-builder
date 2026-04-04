"""Pipeline version history endpoints."""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import Feed, PipelineVersion, create_pipeline_version, get_db

router = APIRouter(prefix="/feeds", tags=["pipeline_versions"])
logger = logging.getLogger(__name__)


def _version_to_dict(v: PipelineVersion) -> dict[str, Any]:
    config = None
    try:
        config = json.loads(v.config_json) if v.config_json else None
    except Exception:
        pass
    return {
        "id": v.id,
        "feed_id": v.feed_id,
        "version_number": v.version_number,
        "is_active": v.is_active,
        "has_been_replayed": bool(v.has_been_replayed),
        "label": v.label,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "config": config,
    }


@router.get("/{feed_id}/pipeline-versions")
def list_pipeline_versions(
    feed_id: str,
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    versions = (
        db.query(PipelineVersion)
        .filter(PipelineVersion.feed_id == feed_id)
        .order_by(PipelineVersion.version_number.desc())
        .all()
    )
    return [_version_to_dict(v) for v in versions]


@router.post("/{feed_id}/pipeline-versions/{version_id}/revert")
def revert_pipeline_version(
    feed_id: str,
    version_id: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    target = db.get(PipelineVersion, version_id)
    if target is None or target.feed_id != feed_id:
        raise HTTPException(status_code=404, detail="Pipeline version not found")

    if target.is_active:
        raise HTTPException(status_code=400, detail="This version is already active")

    new_version = create_pipeline_version(
        feed_id,
        target.config_json,
        db,
        label=f"Reverted to v{target.version_number}",
    )
    # Mirror to feed.config_json so polling and everything else stays consistent
    feed.config_json = target.config_json
    db.commit()
    db.refresh(new_version)
    db.refresh(feed)

    return {
        "version": _version_to_dict(new_version),
        "feed": {
            "id": feed.id,
            "name": feed.name,
            "topic": feed.topic,
            "status": feed.status,
            "config": json.loads(feed.config_json) if feed.config_json else None,
        },
    }
