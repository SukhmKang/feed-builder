"""APScheduler-based feed polling scheduler."""

import asyncio
import json
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.database import Article, Feed, PushSubscription, SessionLocal
from app.services.article_fetcher import fetch_and_filter
from app.services.push import send_push

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def poll_feed_by_id(feed_id: str) -> None:
    db = SessionLocal()
    try:
        feed = db.get(Feed, feed_id)
        if feed is None or feed.status != "ready" or not feed.config_json:
            return
        await _poll_feed(feed, db)
    except Exception:
        logger.exception("Error polling feed %s", feed_id)
    finally:
        db.close()


async def _poll_feed(feed: Feed, db) -> None:
    logger.info("Polling feed %s (%s)", feed.id, feed.topic)
    config = json.loads(feed.config_json)

    results = await fetch_and_filter(config)
    now = datetime.utcnow()
    new_passing: list[dict] = []

    for item in results:
        article = item["article"]
        article_url_hash = str(article.get("id", "")).strip()
        if not article_url_hash:
            continue

        record_id = f"{feed.id}:{article_url_hash}"
        existing = db.get(Article, record_id)
        if existing is not None:
            continue  # already stored

        db_article = Article(
            id=record_id,
            feed_id=feed.id,
            article_json=json.dumps(article),
            passed=item["passed"],
            pipeline_result_json=json.dumps(item["pipeline_result"]),
            fetched_at=now,
            notified=False,
        )
        db.add(db_article)

        if item["passed"]:
            new_passing.append(article)

    feed.last_polled_at = now
    db.commit()

    if new_passing and feed.notifications_enabled:
        await _notify_subscribers(feed, new_passing, db)


async def _notify_subscribers(feed: Feed, articles: list[dict], db) -> None:
    subs = db.query(PushSubscription).filter(PushSubscription.feed_id == feed.id).all()
    if not subs:
        return

    count = len(articles)
    first_title = articles[0].get("title", "New article")
    payload = {
        "title": f"{feed.name}: {count} new article{'s' if count > 1 else ''}",
        "body": first_title if count == 1 else f"{first_title} and {count - 1} more",
        "feedId": feed.id,
    }

    tasks = [send_push(json.loads(sub.subscription_json), payload) for sub in subs]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for sub, result in zip(subs, results):
        if isinstance(result, Exception):
            logger.warning("Push failed for subscription %s: %s", sub.id, result)


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
