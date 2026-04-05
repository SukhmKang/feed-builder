"""Worker service — runs all background jobs and hosts the APScheduler."""

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import BackgroundTasks, FastAPI
from pydantic import BaseModel

from app.database import create_tables
from app.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Feed Builder Worker", lifespan=lifespan)


# ─── Request models ──────────────────────────────────────────────────────────


class BuildFeedRequest(BaseModel):
    feed_id: str
    topic: str
    max_iterations: int = 2


class RunAuditRequest(BaseModel):
    feed_id: str
    start: datetime
    end: datetime
    enable_replay: bool = True
    enable_discovery: bool = True


class ReplayRequest(BaseModel):
    feed_id: str
    version_id: str
    lookback_days: int | None = None


class PollRequest(BaseModel):
    feed_id: str
    lookback_hours: int | None = None


# ─── Endpoints ───────────────────────────────────────────────────────────────


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/build-feed", status_code=202)
async def build_feed_endpoint(req: BuildFeedRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    background_tasks.add_task(_run_build_feed_job, req.feed_id, req.topic, req.max_iterations)
    return {"status": "accepted", "feed_id": req.feed_id}


@app.post("/run-audit", status_code=202)
async def run_audit_endpoint(req: RunAuditRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    background_tasks.add_task(
        _run_audit_job,
        feed_id=req.feed_id,
        start=req.start,
        end=req.end,
        enable_replay=req.enable_replay,
        enable_discovery=req.enable_discovery,
    )
    return {"status": "accepted", "feed_id": req.feed_id}


@app.post("/replay", status_code=202)
async def replay_endpoint(req: ReplayRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    background_tasks.add_task(_run_replay_job, req.feed_id, req.version_id, req.lookback_days)
    return {"status": "accepted", "feed_id": req.feed_id}


@app.post("/poll", status_code=202)
async def poll_endpoint(req: PollRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    from app.scheduler import poll_feed_by_id

    background_tasks.add_task(poll_feed_by_id, req.feed_id, req.lookback_hours)
    return {"status": "accepted", "feed_id": req.feed_id}


# ─── Job implementations ─────────────────────────────────────────────────────


async def _run_build_feed_job(feed_id: str, topic: str, max_iterations: int) -> None:
    from app.agents.pipeline_agent.orchestrator import build_feed_config
    from app.database import Feed, SessionLocal, create_pipeline_version
    from app.pipeline.llm_batching import compile_llm_filter_batches

    db = SessionLocal()
    try:
        feed = db.get(Feed, feed_id)
        if feed is None:
            return
    finally:
        db.close()

    try:
        logger.info("build_feed_job start feed_id=%s topic=%r", feed_id, topic)
        result = await build_feed_config(topic, max_iterations=max_iterations)
        blocks_json = result.get("blocks_json", [])
        if isinstance(blocks_json, list):
            compiled_blocks = await compile_llm_filter_batches(blocks_json)
            result["blocks_json"] = compiled_blocks
            final_config = result.get("final_config")
            if isinstance(final_config, dict):
                final_config["blocks"] = compiled_blocks
    except Exception as exc:
        logger.exception("build_feed_job failed feed_id=%s topic=%r", feed_id, topic)
        db = SessionLocal()
        try:
            feed = db.get(Feed, feed_id)
            if feed is None:
                return
            feed.status = "error"
            feed.error_message = str(exc)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("build_feed_job failed to persist error state feed_id=%s", feed_id)
        finally:
            db.close()
        return

    db = SessionLocal()
    try:
        feed = db.get(Feed, feed_id)
        if feed is None:
            return
        feed.config_json = json.dumps(result.get("final_config", {}))
        feed.agent_output_json = json.dumps(result)
        feed.name = topic[:80]
        feed.status = "ready"
        feed.error_message = None
        create_pipeline_version(feed_id, feed.config_json, db, label="Initial version")
        db.commit()
        logger.info("build_feed_job done feed_id=%s", feed_id)
    except Exception:
        db.rollback()
        logger.exception("build_feed_job failed during persistence feed_id=%s topic=%r", feed_id, topic)
        error_text = "Failed to persist build result"
        db.close()
        db = SessionLocal()
        try:
            feed = db.get(Feed, feed_id)
            if feed is not None:
                feed.status = "error"
                feed.error_message = error_text
                db.commit()
        except Exception:
            db.rollback()
            logger.exception("build_feed_job failed to persist persistence error feed_id=%s", feed_id)
        finally:
            db.close()
        return
    finally:
        db.close()


async def _run_audit_job(
    *,
    feed_id: str,
    start: datetime,
    end: datetime,
    enable_replay: bool,
    enable_discovery: bool,
) -> None:
    from app.agents.audit_agent.orchestrator import run_and_persist_audit

    try:
        audit_id = await run_and_persist_audit(
            feed_id,
            start=start,
            end=end,
            enable_replay=enable_replay,
            enable_discovery=enable_discovery,
        )
        logger.info("audit_job complete audit_id=%s feed_id=%s", audit_id, feed_id)
    except Exception:
        logger.exception("audit_job failed feed_id=%s", feed_id)


async def _run_replay_job(feed_id: str, version_id: str, lookback_days: int | None) -> None:
    from datetime import timedelta

    from sqlalchemy import tuple_

    from app.database import Article, PipelineVersion, SessionLocal, StoryArticle
    from app.pipeline.llm_batching import run_pipeline_batch
    from app.pipeline.schema import deserialize_pipeline
    from app.services.stories import assign_article_to_story, remove_article_from_stories

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
            logger.info("replay_job feed_id=%s no sources in active version, skipping", feed_id)
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
            logger.info("replay_job feed_id=%s no matching articles found", feed_id)
            return

        logger.info("replay_job feed_id=%s re-evaluating %d articles", feed_id, len(raw_articles))
        blocks = deserialize_pipeline(config.get("blocks", []))

        old_passed: dict[str, bool] = {
            str(json.loads(r.article_json).get("id", "")).strip(): bool(r.passed)
            for r in records
            if r.article_json
        }

        results = await run_pipeline_batch(raw_articles, blocks)

        newly_passed: list[tuple] = []
        newly_failed: list[str] = []

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
            "replay_job feed_id=%s done, updated %d articles, %d newly passed, %d newly failed",
            feed_id, len(results), len(newly_passed), len(newly_failed),
        )

        for article_db_id in newly_failed:
            try:
                await remove_article_from_stories(db, article_id=article_db_id)
            except Exception:
                logger.warning("replay_job: story removal failed for %s", article_db_id)

        for record, article_payload in newly_passed:
            existing_link = db.query(StoryArticle).filter(StoryArticle.article_id == record.id).first()
            if not existing_link:
                try:
                    await assign_article_to_story(
                        db, feed_id=feed_id, article_record=record, article_payload=article_payload
                    )
                except Exception:
                    logger.warning("replay_job: story assignment failed for %s", record.id)
    except Exception:
        logger.exception("replay_job feed_id=%s failed", feed_id)
        db.rollback()
    finally:
        db.close()
