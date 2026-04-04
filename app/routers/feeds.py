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

from app.database import Article, Feed, PipelineVersion, create_pipeline_version, get_db
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
    poll_interval_hours: int | None = None
    blocks: list | None = None
    sources: list[dict[str, Any]] | None = None
    version_label: str | None = None


class ReplayFeedRequest(BaseModel):
    lookback_days: int | None = None  # None = all time


class BlockEditRequest(BaseModel):
    block: dict[str, Any]
    sources: list[dict[str, Any]]
    block_path: str
    parent_context: str
    sibling_blocks: list[dict[str, Any]] = []
    instruction: str


def _feed_to_dict(feed: Feed, *, active_version_replayed: bool = False) -> dict[str, Any]:
    return {
        "id": feed.id,
        "name": feed.name,
        "topic": feed.topic,
        "status": feed.status,
        "poll_interval_hours": feed.poll_interval_hours,
        "created_at": feed.created_at.isoformat() if feed.created_at else None,
        "last_polled_at": feed.last_polled_at.isoformat() if feed.last_polled_at else None,
        "error_message": feed.error_message,
        "config": json.loads(feed.config_json) if feed.config_json else None,
        "active_version_replayed": active_version_replayed,
    }


def _get_active_version_replayed(feed_id: str, db) -> bool:
    v = db.query(PipelineVersion).filter(
        PipelineVersion.feed_id == feed_id,
        PipelineVersion.is_active.is_(True),
    ).first()
    return bool(v.has_been_replayed) if v else False


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
            create_pipeline_version(feed_id, feed.config_json, db, label="Initial version")
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
    feed_ids = [f.id for f in feeds]
    replayed_feed_ids = {
        v.feed_id
        for v in db.query(PipelineVersion).filter(
            PipelineVersion.feed_id.in_(feed_ids),
            PipelineVersion.is_active.is_(True),
            PipelineVersion.has_been_replayed.is_(True),
        ).all()
    }
    return [_feed_to_dict(f, active_version_replayed=f.id in replayed_feed_ids) for f in feeds]


@router.get("/custom-blocks")
def list_custom_blocks() -> list[dict[str, str | None]]:
    return _list_custom_block_metadata()


@router.get("/{feed_id}")
def get_feed(feed_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")
    return _feed_to_dict(feed, active_version_replayed=_get_active_version_replayed(feed_id, db))


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

    if req.blocks is not None or req.sources is not None:
        create_pipeline_version(feed_id, feed.config_json, db, label=req.version_label)

    db.commit()
    db.refresh(feed)
    return _feed_to_dict(feed, active_version_replayed=_get_active_version_replayed(feed_id, db))


@router.post("/{feed_id}/ai-edit-block")
async def ai_edit_block(
    feed_id: str,
    req: BlockEditRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from app.agents.pipeline_agent.runtime import DEFAULT_AGENT_MODEL, run_block_edit_agent
    from app.agents.pipeline_agent.source_specs import validate_source_spec
    from app.pipeline.schema import deserialize_block

    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    instruction = str(req.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=422, detail="Instruction must be non-empty")
    try:
        current_sources = [
            validate_source_spec(source, label=f"sources[{index}]")
            for index, source in enumerate(req.sources)
        ]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        deserialize_block(req.block)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid block: {exc}") from exc
    try:
        for index, sibling in enumerate(req.sibling_blocks):
            deserialize_block(sibling)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid sibling block: {exc}") from exc
    block_path = str(req.block_path or "").strip()
    parent_context = str(req.parent_context or "").strip()
    if not block_path:
        raise HTTPException(status_code=422, detail="block_path must be non-empty")
    if not parent_context:
        raise HTTPException(status_code=422, detail="parent_context must be non-empty")

    try:
        replacement_blocks = await run_block_edit_agent(
            feed.topic,
            current_sources,
            req.block,
            block_path=block_path,
            parent_context=parent_context,
            sibling_blocks_json=req.sibling_blocks,
            instruction=instruction,
            model=DEFAULT_AGENT_MODEL,
            verbose=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"replacement_blocks": replacement_blocks}


@router.delete("/{feed_id}", status_code=204)
def delete_feed(feed_id: str, db: Session = Depends(get_db)) -> None:
    from app.database import Article, Story, StoryArticle

    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")
    story_ids = [story.id for story in db.query(Story).filter(Story.feed_id == feed_id).all()]
    if story_ids:
        db.query(StoryArticle).filter(StoryArticle.story_id.in_(story_ids)).delete(synchronize_session=False)
    db.query(Story).filter(Story.feed_id == feed_id).delete(synchronize_session=False)
    db.query(Article).filter(Article.feed_id == feed_id).delete()
    db.delete(feed)
    db.commit()


@router.post("/{feed_id}/replay", status_code=202)
async def replay_feed(
    feed_id: str,
    req: ReplayFeedRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")
    if feed.status != "ready":
        raise HTTPException(status_code=409, detail="Feed is not ready")

    active_version = db.query(PipelineVersion).filter(
        PipelineVersion.feed_id == feed_id,
        PipelineVersion.is_active.is_(True),
    ).first()
    if active_version is None:
        raise HTTPException(status_code=409, detail="No active pipeline version")
    if active_version.has_been_replayed:
        raise HTTPException(status_code=409, detail="Current pipeline version has already been replayed")

    # Mark replayed immediately to prevent duplicate requests racing the background task
    active_version.has_been_replayed = True
    db.commit()

    background_tasks.add_task(_run_replay_task, feed_id, active_version.id, req.lookback_days)
    return {"status": "replaying"}


async def _run_replay_task(feed_id: str, version_id: str, lookback_days: int | None) -> None:
    """Re-evaluate all articles from current sources against the current pipeline in-memory."""
    import json
    from datetime import timedelta

    from sqlalchemy import tuple_

    from app.database import SessionLocal
    from app.pipeline.llm_batching import run_pipeline_batch
    from app.pipeline.schema import deserialize_pipeline

    db = SessionLocal()
    try:
        version = db.get(PipelineVersion, version_id)
        if version is None:
            return
        config = json.loads(version.config_json)
        active_source_keys = [
            (s["type"], s["feed"]) for s in config.get("sources", [])
            if s.get("type") and s.get("feed")
        ]
        if not active_source_keys:
            logger.info("replay_task feed_id=%s no sources in active version, skipping", feed_id)
            return

        query = db.query(Article).filter(
            Article.feed_id == feed_id,
            tuple_(Article.spec_source_type, Article.spec_source_feed).in_(active_source_keys),
        )
        if lookback_days is not None:
            cutoff = datetime.utcnow() - timedelta(days=lookback_days)
            query = query.filter(Article.fetched_at >= cutoff)
        records = query.all()

        raw_articles: list[dict] = []
        record_map: dict[str, Article] = {}
        for r in records:
            try:
                a = json.loads(r.article_json)
            except Exception:
                continue
            raw_articles.append(a)
            record_map[str(a.get("id", "")).strip()] = r

        if not raw_articles:
            logger.info("replay_task feed_id=%s no matching articles found", feed_id)
            return

        logger.info("replay_task feed_id=%s re-evaluating %d articles", feed_id, len(raw_articles))
        blocks = deserialize_pipeline(config.get("blocks", []))

        # Capture pass state BEFORE updating so we can detect changes
        old_passed: dict[str, bool] = {
            str(json.loads(r.article_json).get("id", "")).strip(): bool(r.passed)
            for r in records
            if r.article_json
        }

        results = await run_pipeline_batch(raw_articles, blocks)

        newly_passed: list[tuple] = []  # (record, article_payload)
        newly_failed: list[str] = []   # record.id values

        for result in results:
            article_id = str(result["article"].get("id", "")).strip()
            record = record_map.get(article_id)
            if not record:
                continue
            was_passed = old_passed.get(article_id, False)
            now_passed = result["passed"]
            record.passed = now_passed
            record.pipeline_result_json = json.dumps({
                "passed": now_passed,
                "block_results": result.get("block_results", []),
                "dropped_at": result.get("dropped_at"),
            })
            record.pipeline_version_id = version_id
            if not was_passed and now_passed:
                newly_passed.append((record, result["article"]))
            elif was_passed and not now_passed:
                newly_failed.append(record.id)

        db.commit()
        logger.info(
            "replay_task feed_id=%s done, updated %d articles, %d newly passed, %d newly failed",
            feed_id, len(results), len(newly_passed), len(newly_failed),
        )

        # Sync story membership — removals first (cheap), then assignments
        from app.database import StoryArticle
        from app.services.stories import assign_article_to_story, remove_article_from_stories

        for article_db_id in newly_failed:
            try:
                await remove_article_from_stories(db, article_id=article_db_id)
            except Exception:
                logger.warning("replay_task: story removal failed for %s", article_db_id)

        for record, article_payload in newly_passed:
            existing_link = db.query(StoryArticle).filter(StoryArticle.article_id == record.id).first()
            if not existing_link:
                try:
                    await assign_article_to_story(
                        db, feed_id=feed_id, article_record=record, article_payload=article_payload
                    )
                except Exception:
                    logger.warning("replay_task: story assignment failed for %s", record.id)
    except Exception:
        logger.exception("replay_task feed_id=%s failed", feed_id)
        db.rollback()
    finally:
        db.close()


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
