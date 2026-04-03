"""Feed CRUD endpoints."""

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import Article, Feed, get_db
from app.services.feed_builder import build_feed

router = APIRouter(prefix="/feeds", tags=["feeds"])
logger = logging.getLogger(__name__)
CUSTOM_BLOCKS_DIR = Path(__file__).resolve().parent.parent / "custom_blocks"
CUSTOM_BLOCKS_REGISTRY_DB = CUSTOM_BLOCKS_DIR / "_registry.db"
FEED_RSS_ARTICLE_LIMIT = 200


class CreateFeedRequest(BaseModel):
    topic: str
    poll_interval_hours: int = 24


class UpdateFeedRequest(BaseModel):
    name: str | None = None
    notifications_enabled: bool | None = None
    poll_interval_hours: int | None = None
    blocks: list | None = None
    sources: list[dict[str, Any]] | None = None


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


def _article_sort_key(record: Article) -> tuple[datetime, datetime]:
    article = json.loads(record.article_json) if record.article_json else {}
    published_at = _parse_sort_datetime(article.get("published_at"))
    fetched_at = _parse_sort_datetime(record.fetched_at.isoformat() if record.fetched_at else None)
    return (published_at, fetched_at)


def _parse_sort_datetime(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.min

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min

    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def _build_feed_rss_xml(feed: Feed, request: Request, articles: list[Article]) -> str:
    items: list[str] = []
    for record in articles:
        payload = json.loads(record.article_json) if record.article_json else {}
        title = escape(str(payload.get("title", "")).strip() or "Untitled article")
        link = escape(str(payload.get("url", "")).strip())
        guid = escape(str(payload.get("id", "")).strip() or record.id)
        description = escape(str(payload.get("content", "")).strip() or str(payload.get("full_text", "")).strip())
        source_name = escape(str(payload.get("source_name", "")).strip())
        published_at = str(payload.get("published_at", "")).strip()
        pub_date = ""
        if published_at:
            try:
                pub_date = _parse_sort_datetime(published_at).strftime("%a, %d %b %Y %H:%M:%S GMT")
            except Exception:
                pub_date = ""

        parts = [
            "<item>",
            f"<title>{title}</title>",
            f"<link>{link}</link>" if link else "",
            f'<guid isPermaLink="false">{guid}</guid>',
            f"<description>{description}</description>" if description else "",
            f"<source>{source_name}</source>" if source_name else "",
            f"<pubDate>{pub_date}</pubDate>" if pub_date else "",
            "</item>",
        ]
        items.append("".join(parts))

    self_link = escape(str(request.url))
    channel_title = escape(feed.name)
    channel_description = escape(feed.topic)
    last_build = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0">'
        "<channel>"
        f"<title>{channel_title}</title>"
        f"<description>{channel_description}</description>"
        f"<link>{self_link}</link>"
        f"<lastBuildDate>{last_build}</lastBuildDate>"
        + "".join(items)
        + "</channel>"
        "</rss>"
    )


def _list_custom_block_metadata() -> list[dict[str, str | None]]:
    names = sorted(
        path.stem
        for path in CUSTOM_BLOCKS_DIR.glob("*.py")
        if path.is_file() and path.stem not in {"__init__"}
    )

    metadata_by_name: dict[str, dict[str, str | None]] = {}
    if CUSTOM_BLOCKS_REGISTRY_DB.exists():
        connection = sqlite3.connect(CUSTOM_BLOCKS_REGISTRY_DB)
        try:
            rows = connection.execute(
                "SELECT name, title, description FROM custom_blocks"
            ).fetchall()
        finally:
            connection.close()

        for name, title, description in rows:
            normalized = str(name or "").strip()
            if not normalized:
                continue
            metadata_by_name[normalized] = {
                "title": str(title).strip() if title is not None else None,
                "description": str(description).strip() if description is not None else None,
            }

    return [
        {
            "name": name,
            "title": metadata_by_name.get(name, {}).get("title"),
            "description": metadata_by_name.get(name, {}).get("description"),
        }
        for name in names
    ]


async def _run_build_feed(feed_id: str, topic: str) -> None:
    """Background task: run the pipeline agent and update the feed record."""
    from app.database import SessionLocal
    from app.pipeline.llm_batching import compile_llm_filter_batches

    db = SessionLocal()
    try:
        feed = db.get(Feed, feed_id)
        if feed is None:
            return
        try:
            logger.info("Feed build background task start feed_id=%s topic=%r", feed_id, topic)
            result = await build_feed(topic)
            logger.info(
                "Feed build background task build_feed returned feed_id=%s merged_sources=%s blocks=%s",
                feed_id,
                len(result.get("merged_sources", []) or []),
                len(result.get("blocks_json", []) or []),
            )
            blocks_json = result.get("blocks_json", [])
            if isinstance(blocks_json, list):
                logger.info("Feed build batch prompt compile start feed_id=%s block_count=%s", feed_id, len(blocks_json))
                compiled_blocks = await compile_llm_filter_batches(blocks_json)
                logger.info("Feed build batch prompt compile done feed_id=%s block_count=%s", feed_id, len(compiled_blocks))
                result["blocks_json"] = compiled_blocks
                final_config = result.get("final_config")
                if isinstance(final_config, dict):
                    final_config["blocks"] = compiled_blocks
            feed.config_json = json.dumps(result.get("final_config", {}))
            feed.agent_output_json = json.dumps(result)
            feed.name = topic[:80]
            feed.status = "ready"
            logger.info("Feed build background task marked ready feed_id=%s", feed_id)
        except Exception as exc:
            feed.status = "error"
            feed.error_message = str(exc)
            logger.exception("Feed build background task failed feed_id=%s topic=%r", feed_id, topic)
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


@router.get("/custom-blocks")
def list_custom_blocks() -> list[dict[str, str | None]]:
    return _list_custom_block_metadata()


@router.get("/{feed_id}")
def get_feed(feed_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")
    return _feed_to_dict(feed)


@router.get("/{feed_id}/rss")
def get_feed_rss(
    feed_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    articles = (
        db.query(Article)
        .filter(Article.feed_id == feed_id, Article.passed.is_(True))
        .all()
    )
    articles.sort(key=_article_sort_key, reverse=True)
    xml = _build_feed_rss_xml(feed, request, articles[:FEED_RSS_ARTICLE_LIMIT])
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")


@router.patch("/{feed_id}")
async def update_feed(
    feed_id: str,
    req: UpdateFeedRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")
    if req.name is not None:
        normalized_name = str(req.name).strip()
        if not normalized_name:
            raise HTTPException(status_code=422, detail="Feed name must be non-empty")
        feed.name = normalized_name[:120]
    if req.notifications_enabled is not None:
        feed.notifications_enabled = req.notifications_enabled
    if req.poll_interval_hours is not None:
        feed.poll_interval_hours = req.poll_interval_hours
    if req.blocks is not None:
        from app.pipeline.llm_batching import compile_llm_filter_batches
        from app.pipeline.schema import is_valid_pipeline_definition
        if not is_valid_pipeline_definition(req.blocks):
            raise HTTPException(status_code=422, detail="Invalid pipeline definition")
        compiled_blocks = await compile_llm_filter_batches(req.blocks)
        config = json.loads(feed.config_json) if feed.config_json else {}
        config["blocks"] = compiled_blocks
        feed.config_json = json.dumps(config)
    if req.sources is not None:
        from app.agents.pipeline_agent.source_specs import validate_source_spec

        validated_sources = []
        try:
            for index, source in enumerate(req.sources):
                validated_sources.append(validate_source_spec(source, label=f"sources[{index}]"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        config = json.loads(feed.config_json) if feed.config_json else {}
        config["sources"] = validated_sources
        feed.config_json = json.dumps(config)
    db.commit()
    db.refresh(feed)
    return _feed_to_dict(feed)


@router.delete("/{feed_id}", status_code=204)
def delete_feed(feed_id: str, db: Session = Depends(get_db)) -> None:
    from app.database import Article, PushSubscription, Story, StoryArticle

    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")
    story_ids = [story.id for story in db.query(Story).filter(Story.feed_id == feed_id).all()]
    if story_ids:
        db.query(StoryArticle).filter(StoryArticle.story_id.in_(story_ids)).delete(synchronize_session=False)
    db.query(Story).filter(Story.feed_id == feed_id).delete(synchronize_session=False)
    db.query(Article).filter(Article.feed_id == feed_id).delete()
    db.query(PushSubscription).filter(PushSubscription.feed_id == feed_id).delete()
    db.delete(feed)
    db.commit()


@router.post("/{feed_id}/poll", status_code=202)
async def trigger_poll(
    feed_id: str,
    background_tasks: BackgroundTasks,
    lookback_hours: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")
    if feed.status != "ready":
        raise HTTPException(status_code=400, detail="Feed is not ready")
    from app.scheduler import poll_feed_by_id

    logger.info(
        "Manual poll queued for feed %s (%s) lookback_hours=%s",
        feed.id,
        feed.topic,
        lookback_hours,
    )
    background_tasks.add_task(poll_feed_by_id, feed_id, lookback_hours)
    return {"status": "polling"}
