"""Collect and aggregate article data for the audit agent."""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import tuple_

from app.database import Article, PipelineVersion, SessionLocal

from .types import AggregateStats, SourceStats, WeeklyBucket

logger = logging.getLogger(__name__)


def _effective_passed(article: dict[str, Any], db_passed_default: bool | None = None) -> bool:
    manual_verdict = str(article.get("_manual_verdict") or "").strip().lower()
    if manual_verdict == "passed":
        return True
    if manual_verdict == "filtered":
        return False
    if db_passed_default is not None:
        return db_passed_default
    return bool(article.get("passed"))


async def collect_audit_data(
    feed_id: str,
    start: datetime,
    end: datetime,
    *,
    db=None,
    enable_replay: bool = True,
) -> tuple[AggregateStats, list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect article data for the audit period.

    When enable_replay=True, all articles from the current sources are re-evaluated
    against the current active pipeline in-memory, giving the audit agent a full
    picture of how the current pipeline performs on all available historical data.

    When enable_replay=False, only articles evaluated under the current active
    pipeline version (within the period) are returned.

    Returns
    -------
    (stats, passed, filtered)
    """
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        return await _collect(feed_id, start, end, db=db, enable_replay=enable_replay)
    finally:
        if own_db:
            db.close()


async def _collect(
    feed_id: str,
    start: datetime,
    end: datetime,
    *,
    db,
    enable_replay: bool,
) -> tuple[AggregateStats, list[dict[str, Any]], list[dict[str, Any]]]:
    active_version = db.query(PipelineVersion).filter(
        PipelineVersion.feed_id == feed_id,
        PipelineVersion.is_active.is_(True),
    ).first()

    if enable_replay:
        passed, filtered = await _collect_with_replay(feed_id, active_version, db)
    else:
        passed, filtered = _collect_from_db(feed_id, start, end, active_version, db)

    logger.info(
        "audit.data_collector feed_id=%s period=%s→%s enable_replay=%s passed=%s filtered=%s",
        feed_id,
        start.isoformat(),
        end.isoformat(),
        enable_replay,
        len(passed),
        len(filtered),
    )

    all_articles = passed + filtered
    stats = _compute_aggregate_stats(
        all_articles,
        passed=passed,
        feed_id=feed_id,
        start=start,
        end=end,
    )
    return stats, passed, filtered


def _collect_from_db(
    feed_id: str,
    start: datetime,
    end: datetime,
    active_version: PipelineVersion | None,
    db,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return articles in the period that belong to the active pipeline version."""
    query = db.query(Article).filter(
        Article.feed_id == feed_id,
        Article.fetched_at >= start,
        Article.fetched_at <= end,
    )
    if active_version:
        from sqlalchemy import or_
        query = query.filter(
            or_(
                Article.pipeline_version_id == active_version.id,
                Article.pipeline_version_id.is_(None),
            )
        )
    records = query.order_by(Article.fetched_at.asc()).all()

    passed: list[dict[str, Any]] = []
    filtered: list[dict[str, Any]] = []
    for record in records:
        try:
            article = json.loads(record.article_json)
        except Exception:
            continue
        try:
            article["_pipeline_result"] = json.loads(record.pipeline_result_json or "{}")
        except Exception:
            article["_pipeline_result"] = {}
        article["_manual_verdict"] = getattr(record, "manual_verdict", None)
        article["_pipeline_passed"] = bool(record.passed)

        if _effective_passed(article, db_passed_default=bool(record.passed)):
            passed.append(article)
        else:
            filtered.append(article)

    return passed, filtered


async def _collect_with_replay(
    feed_id: str,
    active_version: PipelineVersion | None,
    db,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Re-evaluate all DB articles from current sources against the current pipeline."""
    if active_version is None:
        logger.warning("audit.replay feed_id=%s has no active version; falling back to all articles", feed_id)
        # No active version — fall back to all articles without re-evaluation
        all_records = db.query(Article).filter(Article.feed_id == feed_id).all()
        passed, filtered = [], []
        for record in all_records:
            try:
                article = json.loads(record.article_json)
            except Exception:
                continue
            try:
                article["_pipeline_result"] = json.loads(record.pipeline_result_json or "{}")
            except Exception:
                article["_pipeline_result"] = {}
            article["_manual_verdict"] = getattr(record, "manual_verdict", None)
            if _effective_passed(article, db_passed_default=bool(record.passed)):
                passed.append(article)
            else:
                filtered.append(article)
        return passed, filtered

    config = json.loads(active_version.config_json)
    current_sources = config.get("sources", [])

    # Build a set of (spec_source_type, spec_source_feed) pairs for current sources
    active_source_keys = {
        (s["type"], s["feed"]) for s in current_sources
        if s.get("type") and s.get("feed")
    }

    # Fetch only articles from sources that exist in the current version
    if active_source_keys:
        spec_pairs = list(active_source_keys)
        matching_records = (
            db.query(Article)
            .filter(
                Article.feed_id == feed_id,
                tuple_(Article.spec_source_type, Article.spec_source_feed).in_(spec_pairs),
            )
            .all()
        )
    else:
        matching_records = []

    if not matching_records:
        logger.info("audit.replay feed_id=%s no articles matched current sources", feed_id)
        return [], []

    logger.info("audit.replay feed_id=%s re-evaluating %d articles", feed_id, len(matching_records))

    # Deserialize articles and build lookup for manual verdicts
    raw_articles: list[dict[str, Any]] = []
    manual_verdicts: dict[str, str | None] = {}
    record_by_url_hash: dict[str, Article] = {}
    for record in matching_records:
        try:
            article = json.loads(record.article_json)
        except Exception:
            continue
        article_id = str(article.get("id", "")).strip()
        manual_verdicts[article_id] = getattr(record, "manual_verdict", None)
        record_by_url_hash[article_id] = record
        raw_articles.append(article)

    # Re-run current pipeline in-memory
    from app.pipeline.llm_batching import run_pipeline_batch
    from app.pipeline.schema import deserialize_pipeline

    blocks_json = config.get("blocks", [])
    try:
        blocks = deserialize_pipeline(blocks_json)
        results = await run_pipeline_batch(raw_articles, blocks)
    except Exception as exc:
        logger.warning("audit.replay pipeline re-evaluation failed: %s", exc)
        return [], []

    passed: list[dict[str, Any]] = []
    filtered: list[dict[str, Any]] = []

    for result in results:
        article = result["article"]
        article_id = str(article.get("id", "")).strip()
        pipeline_result = {
            "passed": result["passed"],
            "block_results": result.get("block_results", []),
            "dropped_at": result.get("dropped_at"),
        }
        article["_pipeline_result"] = pipeline_result
        article["_manual_verdict"] = manual_verdicts.get(article_id)
        article["_pipeline_passed"] = result["passed"]

        if _effective_passed(article, db_passed_default=result["passed"]):
            passed.append(article)
        else:
            filtered.append(article)

    logger.info(
        "audit.replay feed_id=%s re-evaluated: passed=%d filtered=%d",
        feed_id, len(passed), len(filtered),
    )
    return passed, filtered


def _compute_aggregate_stats(
    all_articles: list[dict[str, Any]],
    *,
    passed: list[dict[str, Any]],
    feed_id: str,
    start: datetime,
    end: datetime,
) -> AggregateStats:
    passed_ids = {str(a.get("id", "")).strip() for a in passed}
    manual_passed_count = 0
    manual_filtered_count = 0
    for article in all_articles:
        manual_verdict = str(article.get("_manual_verdict") or "").strip().lower()
        if manual_verdict not in ("passed", "filtered"):
            continue
        pipeline_passed = article.get("_pipeline_passed")
        if pipeline_passed is None:
            # No pipeline signal — treat as genuine override (safe fallback)
            if manual_verdict == "passed":
                manual_passed_count += 1
            else:
                manual_filtered_count += 1
            continue
        # Only a genuine override if manual tag contradicts the pipeline verdict
        if manual_verdict == "passed" and not pipeline_passed:
            manual_passed_count += 1
        elif manual_verdict == "filtered" and pipeline_passed:
            manual_filtered_count += 1
        # If they agree: redundant tag, don't count as an override

    # Per-source bucketing
    source_buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for article in all_articles:
        src_type = str(article.get("source_type", "")).strip()
        src_name = str(article.get("source_name", "")).strip() or str(article.get("source_url", "")).strip()
        src_feed = str(article.get("source_url", "")).strip()
        key = (src_type, src_name)
        if key not in source_buckets:
            source_buckets[key] = {
                "source_type": src_type,
                "source_feed": src_feed,
                "source_name": src_name,
                "total": 0,
                "passed": 0,
            }
        source_buckets[key]["total"] += 1
        article_id = str(article.get("id", "")).strip()
        if article_id in passed_ids:
            source_buckets[key]["passed"] += 1

    per_source: list[SourceStats] = []
    for bucket in source_buckets.values():
        total = bucket["total"]
        passed_count = bucket["passed"]
        filtered_count = total - passed_count
        per_source.append(
            SourceStats(
                source_type=bucket["source_type"],
                source_feed=bucket["source_feed"],
                source_name=bucket["source_name"],
                total_articles=total,
                passed_count=passed_count,
                filtered_count=filtered_count,
                pass_rate=round(passed_count / total, 4) if total > 0 else 0.0,
            )
        )
    per_source.sort(key=lambda s: s["total_articles"], reverse=True)

    weekly_trend = _compute_weekly_trend(all_articles, passed_ids, start, end)

    total = len(all_articles)
    passed_count = len(passed)
    filtered_count = total - passed_count

    return AggregateStats(
        feed_id=feed_id,
        audit_period_start=start.isoformat(),
        audit_period_end=end.isoformat(),
        total_articles=total,
        passed_count=passed_count,
        filtered_count=filtered_count,
        overall_pass_rate=round(passed_count / total, 4) if total > 0 else 0.0,
        manual_override_count=manual_passed_count + manual_filtered_count,
        manual_passed_count=manual_passed_count,
        manual_filtered_count=manual_filtered_count,
        per_source=per_source,
        weekly_trend=weekly_trend,
    )


def _compute_weekly_trend(
    articles: list[dict[str, Any]],
    passed_ids: set[str],
    start: datetime,
    end: datetime,
) -> list[WeeklyBucket]:
    from datetime import timedelta

    week_map: dict[str, dict[str, Any]] = {}
    for article in articles:
        published_raw = str(article.get("published_at", "")).strip()
        if not published_raw:
            continue
        try:
            dt = _parse_dt(published_raw)
        except Exception:
            continue
        iso_year, iso_week, _ = dt.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"
        if week_key not in week_map:
            monday = datetime.fromisocalendar(iso_year, iso_week, 1)
            week_map[week_key] = {
                "week_label": f"{week_key} ({monday.strftime('%b %-d')})",
                "week_start": monday.strftime("%Y-%m-%d"),
                "total": 0,
                "passed": 0,
            }
        week_map[week_key]["total"] += 1
        article_id = str(article.get("id", "")).strip()
        if article_id in passed_ids:
            week_map[week_key]["passed"] += 1

    buckets: list[WeeklyBucket] = []
    for _key, w in sorted(week_map.items()):
        total = w["total"]
        passed = w["passed"]
        buckets.append(
            WeeklyBucket(
                week_label=w["week_label"],
                week_start=w["week_start"],
                total_articles=total,
                passed_count=passed,
                pass_rate=round(passed / total, 4) if total > 0 else 0.0,
            )
        )
    return buckets


def _parse_dt(value: str) -> datetime:
    value = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


__all__ = ["collect_audit_data"]
