"""Article query endpoints."""

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import Article, Feed, get_db

router = APIRouter(prefix="/feeds/{feed_id}/articles", tags=["articles"])


def _article_to_dict(article: Article) -> dict[str, Any]:
    return {
        "id": article.id,
        "feed_id": article.feed_id,
        "passed": article.passed,
        "fetched_at": article.fetched_at.isoformat() if article.fetched_at else None,
        "article": json.loads(article.article_json) if article.article_json else {},
        "pipeline_result": json.loads(article.pipeline_result_json) if article.pipeline_result_json else {},
    }


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

    q = db.query(Article).filter(Article.feed_id == feed_id)
    if passed is not None:
        q = q.filter(Article.passed == passed)
    q = q.order_by(Article.fetched_at.desc()).offset(offset).limit(limit)
    return [_article_to_dict(a) for a in q.all()]
