"""FastAPI application entry point."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.database import create_tables
from app.routers import articles, audits, feeds, pipeline_versions, reports, stories

# Dev origins are always allowed. Add your Vercel/Netlify URL(s) via FRONTEND_URL.
# Comma-separated for multiple: FRONTEND_URL=https://app.vercel.app,https://demo.vercel.app
_dev_origins = ["http://localhost:5173", "http://localhost:3000"]
_prod_origins = [u for u in (u.strip() for u in os.environ.get("FRONTEND_URL", "").split(",")) if u]
_allowed_origins = _dev_origins + _prod_origins

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    await _run_data_migration()
    yield


def _infer_spec_from_article(
    source_type: str | None,
    source_url: str | None,
    sources: list,
) -> tuple[str | None, str | None]:
    """Reverse-resolve an article's (source_type, source_url) back to a source spec (type, feed)."""
    if not source_type or not source_url:
        return None, None
    from urllib.parse import quote, quote_plus

    for spec in sources:
        stype = str(spec.get("type", "")).strip()
        sfeed = str(spec.get("feed", "")).strip()
        if not stype or not sfeed:
            continue

        if stype == "rss" and source_type == "rss" and source_url == sfeed:
            return stype, sfeed

        if stype == "tavily" and source_type == "tavily":
            if source_url == f"https://app.tavily.com/search?q={sfeed}":
                return stype, sfeed

        if stype == "google_news_search" and source_type == "google_news":
            if source_url == f"https://news.google.com/rss/search?q={quote_plus(sfeed)}":
                return stype, sfeed

        if stype == "reddit_subreddit" and source_type == "reddit":
            normalized = sfeed.strip()
            if normalized.lower().startswith("r/"):
                normalized = normalized[2:]
            if source_url == f"https://www.reddit.com/r/{normalized}/new/.rss?sort=new":
                return stype, sfeed

        if stype == "reddit_search" and source_type == "reddit":
            if source_url == f"https://www.reddit.com/search.rss?q={quote(sfeed)}&sort=new":
                return stype, sfeed

    return None, None


async def _run_data_migration() -> None:
    """Backfill source columns and create version 1 for existing feeds."""
    import json
    from app.database import Article, Feed, PipelineVersion, SessionLocal, create_pipeline_version

    logger = logging.getLogger(__name__)
    db = SessionLocal()
    try:
        # Backfill source_type / source_url from article_json
        articles_missing = (
            db.query(Article)
            .filter(Article.source_type.is_(None))
            .all()
        )
        if articles_missing:
            for article in articles_missing:
                try:
                    data = json.loads(article.article_json or "{}")
                    article.source_type = data.get("source_type")
                    article.source_url = data.get("source_url")
                except Exception:
                    pass
            db.commit()
            logger.info("data_migration: backfilled source columns on %d articles", len(articles_missing))

        # Create version 1 for feeds that have no versions yet
        feeds = db.query(Feed).all()
        created = 0
        for feed in feeds:
            has_version = db.query(PipelineVersion).filter(
                PipelineVersion.feed_id == feed.id
            ).first()
            if has_version or not feed.config_json:
                continue
            version = create_pipeline_version(feed.id, feed.config_json, db, label="Initial version")
            db.flush()
            db.query(Article).filter(
                Article.feed_id == feed.id,
                Article.pipeline_version_id.is_(None),
            ).update({"pipeline_version_id": version.id}, synchronize_session=False)
            created += 1
        if created:
            db.commit()
            logger.info("data_migration: created initial pipeline versions for %d feeds", created)

        # Backfill spec_source_type / spec_source_feed for existing articles
        articles_missing_spec = (
            db.query(Article)
            .filter(Article.spec_source_type.is_(None))
            .all()
        )
        if articles_missing_spec:
            # Build a version-config cache to avoid re-parsing the same config repeatedly
            version_config_cache: dict[str, list] = {}
            spec_updated = 0
            for article in articles_missing_spec:
                vid = article.pipeline_version_id
                if not vid:
                    continue
                if vid not in version_config_cache:
                    version = db.get(PipelineVersion, vid)
                    if version and version.config_json:
                        try:
                            version_config_cache[vid] = json.loads(version.config_json).get("sources", [])
                        except Exception:
                            version_config_cache[vid] = []
                    else:
                        version_config_cache[vid] = []
                sources = version_config_cache[vid]
                stype, sfeed = _infer_spec_from_article(
                    article.source_type, article.source_url, sources
                )
                if stype:
                    article.spec_source_type = stype
                    article.spec_source_feed = sfeed
                    spec_updated += 1
            if spec_updated:
                db.commit()
                logger.info("data_migration: backfilled spec columns on %d articles", spec_updated)
    except Exception:
        logger.exception("data_migration: failed")
        db.rollback()
    finally:
        db.close()


app = FastAPI(title="Feed Builder", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(feeds.router)
app.include_router(articles.router)
app.include_router(stories.router)
app.include_router(audits.router)
app.include_router(pipeline_versions.router)
app.include_router(reports.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
