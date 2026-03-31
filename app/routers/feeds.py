"""Feed CRUD endpoints."""

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import Feed, get_db
from app.services.feed_builder import build_feed

router = APIRouter(prefix="/feeds", tags=["feeds"])


class CreateFeedRequest(BaseModel):
    topic: str
    poll_interval_hours: int = 24


class UpdateFeedRequest(BaseModel):
    notifications_enabled: bool | None = None
    poll_interval_hours: int | None = None


def _feed_to_dict(feed: Feed) -> dict[str, Any]:
    return {
        "id": feed.id,
        "name": feed.name,
        "topic": feed.topic,
        "status": feed.status,
        "notifications_enabled": feed.notifications_enabled,
        "poll_interval_hours": feed.poll_interval_hours,
        "created_at": feed.created_at.isoformat() if feed.created_at else None,
        "last_polled_at": feed.last_polled_at.isoformat() if feed.last_polled_at else None,
        "error_message": feed.error_message,
        "config": json.loads(feed.config_json) if feed.config_json else None,
    }


async def _run_build_feed(feed_id: str, topic: str) -> None:
    """Background task: run the pipeline agent and update the feed record."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        feed = db.get(Feed, feed_id)
        if feed is None:
            return
        try:
            result = await build_feed(topic)
            feed.config_json = json.dumps(result.get("final_config", {}))
            feed.agent_output_json = json.dumps(result)
            feed.name = topic[:80]
            feed.status = "ready"
        except Exception as exc:
            feed.status = "error"
            feed.error_message = str(exc)[:1000]
        db.commit()
    finally:
        db.close()


@router.post("", status_code=202)
async def create_feed(
    req: CreateFeedRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    feed_id = str(uuid.uuid4())
    feed = Feed(
        id=feed_id,
        user_id="default",
        topic=req.topic,
        name=req.topic[:80],
        status="building",
        poll_interval_hours=req.poll_interval_hours,
    )
    db.add(feed)
    db.commit()
    db.refresh(feed)
    background_tasks.add_task(_run_build_feed, feed_id, req.topic)
    return _feed_to_dict(feed)


@router.get("")
def list_feeds(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    feeds = db.query(Feed).filter(Feed.user_id == "default").order_by(Feed.created_at.desc()).all()
    return [_feed_to_dict(f) for f in feeds]


@router.get("/{feed_id}")
def get_feed(feed_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")
    return _feed_to_dict(feed)


@router.patch("/{feed_id}")
def update_feed(
    feed_id: str,
    req: UpdateFeedRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")
    if req.notifications_enabled is not None:
        feed.notifications_enabled = req.notifications_enabled
    if req.poll_interval_hours is not None:
        feed.poll_interval_hours = req.poll_interval_hours
    db.commit()
    db.refresh(feed)
    return _feed_to_dict(feed)


@router.delete("/{feed_id}", status_code=204)
def delete_feed(feed_id: str, db: Session = Depends(get_db)) -> None:
    from app.database import Article, PushSubscription

    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")
    db.query(Article).filter(Article.feed_id == feed_id).delete()
    db.query(PushSubscription).filter(PushSubscription.feed_id == feed_id).delete()
    db.delete(feed)
    db.commit()


@router.post("/{feed_id}/poll", status_code=202)
async def trigger_poll(
    feed_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")
    if feed.status != "ready":
        raise HTTPException(status_code=400, detail="Feed is not ready")
    from app.scheduler import poll_feed_by_id

    background_tasks.add_task(poll_feed_by_id, feed_id)
    return {"status": "polling"}
