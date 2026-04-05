"""Story query endpoints."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import Article, Feed, PipelineVersion, Story, StoryArticle, get_db
from app.services.stories import serialize_story_detail, serialize_story_summary

router = APIRouter(prefix="/feeds/{feed_id}/stories", tags=["stories"])


class UpdateStoryRequest(BaseModel):
    title: str


@router.get("")
def list_stories(
    feed_id: str,
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    from sqlalchemy import exists

    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    # Find replay floor — most recent replayed version
    floor = (
        db.query(PipelineVersion)
        .filter(
            PipelineVersion.feed_id == feed_id,
            PipelineVersion.has_been_replayed.is_(True),
        )
        .order_by(PipelineVersion.version_number.desc())
        .first()
    )

    query = db.query(Story).filter(Story.feed_id == feed_id, Story.status == "active")

    if floor:
        floor_version_ids = [
            v.id
            for v in db.query(PipelineVersion).filter(
                PipelineVersion.feed_id == feed_id,
                PipelineVersion.version_number >= floor.version_number,
            ).all()
        ]
        # Only include stories that have at least one article from floor onwards
        query = query.filter(
            exists().where(
                (StoryArticle.story_id == Story.id)
                & (StoryArticle.article_id == Article.id)
                & Article.pipeline_version_id.in_(floor_version_ids)
            )
        )

    stories = query.order_by(Story.last_published_at.desc(), Story.updated_at.desc()).all()
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


@router.patch("/{story_id}")
def update_story(
    feed_id: str,
    story_id: str,
    req: UpdateStoryRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    story = db.get(Story, story_id)
    if story is None or story.feed_id != feed_id:
        raise HTTPException(status_code=404, detail="Story not found")

    normalized_title = str(req.title).strip()
    if not normalized_title:
        raise HTTPException(status_code=422, detail="Story title must be non-empty")

    story.title = normalized_title[:160]
    db.commit()
    db.refresh(story)
    return serialize_story_summary(db, story)
