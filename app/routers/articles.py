"""Article query endpoints."""

import json
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import Article, Feed, PipelineVersion, StoryArticle, get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feeds/{feed_id}/articles", tags=["articles"])


def _article_to_dict(article: Article) -> dict[str, Any]:
    return {
        "id": article.id,
        "feed_id": article.feed_id,
        "passed": article.passed,
        "manual_verdict": article.manual_verdict,
        "fetched_at": article.fetched_at.isoformat() if article.fetched_at else None,
        "article": json.loads(article.article_json) if article.article_json else {},
        "pipeline_result": json.loads(article.pipeline_result_json) if article.pipeline_result_json else {},
    }


def _parse_sort_datetime(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)

    # Try ISO 8601
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass

    # Fallback: RFC 2822 (e.g. Tavily's published_date before normalization)
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        pass

    return datetime.min.replace(tzinfo=timezone.utc)


def _article_sort_key(payload: dict[str, Any]) -> tuple[datetime, datetime]:
    article = payload.get("article", {})
    published_at = _parse_sort_datetime(article.get("published_at"))
    fetched_at = _parse_sort_datetime(payload.get("fetched_at"))
    return (published_at, fetched_at)


def effective_passed(article: Article) -> bool:
    if article.manual_verdict == "passed":
        return True
    if article.manual_verdict == "filtered":
        return False
    return bool(article.passed)


def query_feed_articles(feed_id: str, db: Session) -> list[Article]:
    """Return articles for a feed, applying the pipeline version floor when a replay exists."""
    floor = (
        db.query(PipelineVersion)
        .filter(
            PipelineVersion.feed_id == feed_id,
            PipelineVersion.has_been_replayed.is_(True),
        )
        .order_by(PipelineVersion.version_number.desc())
        .first()
    )
    if floor:
        version_ids = [
            v.id
            for v in db.query(PipelineVersion).filter(
                PipelineVersion.feed_id == feed_id,
                PipelineVersion.version_number >= floor.version_number,
            ).all()
        ]
        return db.query(Article).filter(
            Article.feed_id == feed_id,
            Article.pipeline_version_id.in_(version_ids),
        ).all()
    return db.query(Article).filter(Article.feed_id == feed_id).all()


@router.get("")
def list_articles(
    feed_id: str,
    passed: bool | None = Query(default=None, description="Filter by pass/fail. Omit for all."),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    rows = query_feed_articles(feed_id, db)
    if passed is not None:
        rows = [article for article in rows if effective_passed(article) == passed]
    articles = [_article_to_dict(a) for a in rows]
    articles.sort(key=_article_sort_key, reverse=True)
    return articles[offset : offset + limit]


class ManualVerdictRequest(BaseModel):
    verdict: str | None  # "passed" | "filtered" | null to clear


@router.patch("/{article_id}/manual-verdict")
async def set_manual_verdict(
    feed_id: str,
    article_id: str,
    body: ManualVerdictRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    article = db.get(Article, article_id)
    if article is None or article.feed_id != feed_id:
        raise HTTPException(status_code=404, detail="Article not found")

    if body.verdict not in (None, "passed", "filtered"):
        raise HTTPException(status_code=422, detail="verdict must be 'passed', 'filtered', or null")

    article.manual_verdict = body.verdict
    db.commit()
    db.refresh(article)

    # Sync story membership based on new effective verdict
    from app.services.stories import assign_article_to_story, remove_article_from_stories

    if effective_passed(article):
        # Article now effectively passes — assign to a story if not already linked
        already_linked = db.query(StoryArticle).filter(
            StoryArticle.article_id == article.id
        ).first()
        if not already_linked:
            try:
                article_payload = json.loads(article.article_json or "{}")
                await assign_article_to_story(
                    db, feed_id=feed_id, article_record=article, article_payload=article_payload
                )
            except Exception:
                logger.warning("set_manual_verdict: story assignment failed for %s", article.id)
    else:
        try:
            await remove_article_from_stories(db, article_id=article.id)
        except Exception:
            logger.warning("set_manual_verdict: story removal failed for %s", article.id)

    return _article_to_dict(article)
