"""Story query endpoints."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import Feed, Story, get_db
from app.services.stories import serialize_story_detail, serialize_story_summary

router = APIRouter(prefix="/feeds/{feed_id}/stories", tags=["stories"])


@router.get("")
def list_stories(
    feed_id: str,
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    stories = (
        db.query(Story)
        .filter(Story.feed_id == feed_id, Story.status == "active")
        .order_by(Story.last_published_at.desc(), Story.updated_at.desc())
        .all()
    )
    return [serialize_story_summary(db, story) for story in stories]


@router.get("/{story_id}")
def get_story(
    feed_id: str,
    story_id: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    story = db.get(Story, story_id)
    if story is None or story.feed_id != feed_id:
        raise HTTPException(status_code=404, detail="Story not found")
    return serialize_story_detail(db, story)
