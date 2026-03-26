import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
STREAM_HISTORY_DB_PATH = PROJECT_ROOT / "stream_history.db"


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(STREAM_HISTORY_DB_PATH)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS stream_history (
            url TEXT PRIMARY KEY,
            article_id TEXT,
            title TEXT,
            published_at TEXT,
            source_name TEXT,
            source_type TEXT,
            source_url TEXT,
            content TEXT,
            full_text TEXT,
            raw_json TEXT,
            article_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS stream_observations (
            url TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_feed TEXT NOT NULL,
            source_spec_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (url, source_type, source_feed)
        )
        """
    )
    return connection


def _serialize_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, default=str)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime(value: str) -> datetime:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError("datetime value must be non-empty")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid datetime: {value}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _write_articles_sync(
    articles: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> None:
    if not articles and not observations:
        return

    now = _utc_now_iso()
    with _connect() as connection:
        for article in articles:
            url = str(article.get("url", "")).strip()
            if not url:
                continue

            raw_value = article.get("raw")
            connection.execute(
                """
                INSERT INTO stream_history (
                    url,
                    article_id,
                    title,
                    published_at,
                    source_name,
                    source_type,
                    source_url,
                    content,
                    full_text,
                    raw_json,
                    article_json,
                    first_seen_at,
                    last_seen_at,
                    seen_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(url) DO UPDATE SET
                    article_id=excluded.article_id,
                    title=excluded.title,
                    published_at=excluded.published_at,
                    source_name=excluded.source_name,
                    source_type=excluded.source_type,
                    source_url=excluded.source_url,
                    content=excluded.content,
                    full_text=excluded.full_text,
                    raw_json=excluded.raw_json,
                    article_json=excluded.article_json,
                    last_seen_at=excluded.last_seen_at,
                    seen_count=stream_history.seen_count + 1
                """,
                (
                    url,
                    str(article.get("id", "")).strip() or None,
                    str(article.get("title", "")).strip() or None,
                    str(article.get("published_at", "")).strip() or None,
                    str(article.get("source_name", "")).strip() or None,
                    str(article.get("source_type", "")).strip() or None,
                    str(article.get("source_url", "")).strip() or None,
                    str(article.get("content", "")).strip() or None,
                    str(article.get("full_text", "")).strip() or None,
                    _serialize_json(raw_value) if raw_value is not None else None,
                    _serialize_json(article),
                    now,
                    now,
                ),
            )

        for observation in observations:
            url = str(observation.get("url", "")).strip()
            source_type = str(observation.get("source_type", "")).strip()
            source_feed = str(observation.get("source_feed", "")).strip()
            if not url or not source_type or not source_feed:
                continue

            connection.execute(
                """
                INSERT INTO stream_observations (
                    url,
                    source_type,
                    source_feed,
                    source_spec_json,
                    first_seen_at,
                    last_seen_at,
                    seen_count
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(url, source_type, source_feed) DO UPDATE SET
                    source_spec_json=excluded.source_spec_json,
                    last_seen_at=excluded.last_seen_at,
                    seen_count=stream_observations.seen_count + 1
                """,
                (
                    url,
                    source_type,
                    source_feed,
                    _serialize_json(observation.get("source_spec", {})),
                    now,
                    now,
                ),
            )
        connection.commit()


def replay_stream_from_cache(source_feed: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    normalized_source_feed = str(source_feed).strip()
    if not normalized_source_feed:
        raise ValueError("source_feed must be non-empty")

    start_dt = _parse_datetime(start_date)
    end_dt = _parse_datetime(end_date)
    if end_dt < start_dt:
        raise ValueError("end_date must be greater than or equal to start_date")

    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT h.article_json, h.published_at, o.first_seen_at
            FROM stream_observations AS o
            JOIN stream_history AS h
              ON h.url = o.url
            WHERE o.source_feed = ?
              AND h.published_at IS NOT NULL
              AND h.published_at != ''
              AND h.published_at >= ?
              AND h.published_at <= ?
            ORDER BY h.published_at ASC, o.first_seen_at ASC, h.url ASC
            """,
            (
                normalized_source_feed,
                start_dt.isoformat(),
                end_dt.isoformat(),
            ),
        ).fetchall()

    articles: list[dict[str, Any]] = []
    for article_json, _, _ in rows:
        try:
            parsed = json.loads(article_json)
        except Exception:
            continue
        if isinstance(parsed, dict):
            articles.append(parsed)
    return articles


__all__ = ["STREAM_HISTORY_DB_PATH", "_write_articles_sync", "replay_stream_from_cache"]
