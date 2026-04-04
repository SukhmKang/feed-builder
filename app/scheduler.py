"""APScheduler-based feed polling scheduler."""

import json
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.database import Article, Feed, PipelineVersion, SessionLocal
from app.services.article_fetcher import iter_fetch_and_filter
from app.services.stories import assign_article_to_story

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def poll_feed_by_id(feed_id: str, lookback_hours: int | None = None) -> None:
    db = SessionLocal()
    try:
        feed = db.get(Feed, feed_id)
        if feed is None:
            logger.warning("Skipping poll for missing feed %s", feed_id)
            return
        if feed.status != "ready":
            logger.warning("Skipping poll for feed %s because status=%s", feed_id, feed.status)
            return
        if not feed.config_json:
            logger.warning("Skipping poll for feed %s because config_json is empty", feed_id)
            return
        logger.info("Starting poll for feed %s (%s)", feed.id, feed.topic)
        await _poll_feed(feed, db, lookback_hours=lookback_hours)
        logger.info("Completed poll for feed %s (%s)", feed.id, feed.topic)
    except Exception:
        logger.exception("Error polling feed %s", feed_id)
        if feed is not None:
            try:
                config = json.loads(feed.config_json) if feed.config_json else {}
                logger.info(
                    "Poll source context for feed %s: %s",
                    feed.id,
                    config.get("sources", []),
                )
            except Exception:
                logger.exception("Failed to log source context for feed %s", feed_id)
    finally:
        db.close()


async def _poll_feed(feed: Feed, db, *, lookback_hours: int | None = None) -> None:
    logger.info("Polling feed %s (%s)", feed.id, feed.topic)
    config = json.loads(feed.config_json)

    now = datetime.utcnow()
    result_count = 0
    inserted_count = 0
    inserted_passed_count = 0
    inserted_filtered_count = 0
    duplicate_count = 0
    missing_id_count = 0
    existing_article_ids = {
        article_id.split(":", 1)[1]
        for (article_id,) in db.query(Article.id).filter(Article.feed_id == feed.id).all()
        if ":" in str(article_id)
    }

    active_version = db.query(PipelineVersion).filter(
        PipelineVersion.feed_id == feed.id,
        PipelineVersion.is_active.is_(True),
    ).first()
    active_version_id = active_version.id if active_version else None

    max_article_age_hours = (
        max(int(lookback_hours), 1)
        if lookback_hours is not None
        else max(int(feed.poll_interval_hours), 1) * 3
    )
    logger.info(
        "Polling feed %s with article freshness window of %s hours%s",
        feed.id,
        max_article_age_hours,
        " (manual override)" if lookback_hours is not None else "",
    )
    logger.info(
        "Polling feed %s with %s existing stored article ids for duplicate prefiltering",
        feed.id,
        len(existing_article_ids),
    )

    async for item in iter_fetch_and_filter(
        config,
        max_article_age_hours=max_article_age_hours,
        existing_article_ids=existing_article_ids,
    ):
        result_count += 1
        article = item["article"]
        article_url_hash = str(article.get("id", "")).strip()
        if not article_url_hash:
            missing_id_count += 1
            continue

        record_id = f"{feed.id}:{article_url_hash}"
        existing = db.get(Article, record_id)
        if existing is not None:
            duplicate_count += 1
            continue  # already stored
        existing_article_ids.add(article_url_hash)

        db_article = Article(
            id=record_id,
            feed_id=feed.id,
            article_json=json.dumps(article),
            passed=item["passed"],
            pipeline_result_json=json.dumps(item["pipeline_result"]),
            fetched_at=now,
            notified=False,
            pipeline_version_id=active_version_id,
            source_type=article.get("source_type"),
            source_url=article.get("source_url"),
            spec_source_type=article.get("spec_source_type"),
            spec_source_feed=article.get("spec_source_feed"),
        )
        db.add(db_article)
        inserted_count += 1

        if item["passed"]:
            inserted_passed_count += 1
        else:
            inserted_filtered_count += 1

        db.commit()
        logger.info(
            "Poll incremental save for feed %s: article_id=%s passed=%s inserted_count=%s",
            feed.id,
            article_url_hash,
            item["passed"],
            inserted_count,
        )
        if item["passed"]:
            try:
                await assign_article_to_story(
                    db,
                    feed_id=feed.id,
                    article_record=db_article,
                    article_payload=article,
                )
                logger.info(
                    "Story assignment completed for feed %s article_id=%s",
                    feed.id,
                    article_url_hash,
                )
            except Exception:
                logger.exception(
                    "Story assignment failed for feed %s article_id=%s",
                    feed.id,
                    article_url_hash,
                )

    feed.last_polled_at = now
    db.commit()
    logger.info(
        "Poll summary for feed %s: results=%s inserted=%s passed=%s filtered=%s duplicates=%s missing_ids=%s",
        feed.id,
        result_count,
        inserted_count,
        inserted_passed_count,
        inserted_filtered_count,
        duplicate_count,
        missing_id_count,
    )

async def _poll_due_feeds() -> None:
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        feeds = db.query(Feed).filter(Feed.status == "ready").all()
        for feed in feeds:
            if feed.last_polled_at is None:
                due = True
            else:
                elapsed_hours = (now - feed.last_polled_at).total_seconds() / 3600
                due = elapsed_hours >= feed.poll_interval_hours
            if due:
                try:
                    await _poll_feed(feed, db)
                except Exception:
                    logger.exception("Error polling feed %s", feed.id)
    finally:
        db.close()


def start_scheduler() -> None:
    scheduler.add_job(_poll_due_feeds, "interval", hours=1, id="poll_due_feeds")
    scheduler.start()


def stop_scheduler() -> None:
    scheduler.shutdown(wait=False)
