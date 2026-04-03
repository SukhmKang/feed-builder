import argparse
import asyncio
import copy
import json
import sqlite3
from typing import Any

from app.pipeline.llm_batching import compile_llm_filter_batches

DEFAULT_FEED_ID = "9425c24e-06db-4bb8-92b4-25b15d7b7298"


def clear_cached_batch_fields(blocks: list[dict[str, Any]]) -> None:
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "llm_filter":
            block.pop("batch_prompt", None)
            block.pop("batch_prompt_source_hash", None)
        if block.get("type") == "conditional":
            clear_cached_batch_fields(block.get("if_true", []))
            clear_cached_batch_fields(block.get("if_false", []))
        if block.get("type") == "switch":
            for branch in block.get("branches", []):
                clear_cached_batch_fields(branch.get("blocks", []))
            clear_cached_batch_fields(block.get("default", []))


def count_llm_filters(blocks: list[dict[str, Any]]) -> int:
    total = 0
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "llm_filter":
            total += 1
        elif block.get("type") == "conditional":
            total += count_llm_filters(block.get("if_true", []))
            total += count_llm_filters(block.get("if_false", []))
        elif block.get("type") == "switch":
            for branch in block.get("branches", []):
                total += count_llm_filters(branch.get("blocks", []))
            total += count_llm_filters(block.get("default", []))
    return total


def _repair_feed(conn: sqlite3.Connection, *, feed_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT name, config_json FROM feeds WHERE id=?", (feed_id,)).fetchone()
    if not row:
        raise SystemExit(f"Feed not found: {feed_id}")

    feed_name, config_json = row
    config = json.loads(config_json) if config_json else {}
    blocks = copy.deepcopy(config.get("blocks", []))
    llm_filter_count = count_llm_filters(blocks)
    if llm_filter_count == 0:
        return {
            "feed_id": feed_id,
            "feed_name": feed_name,
            "recompiled_llm_filters": 0,
            "skipped": True,
        }

    clear_cached_batch_fields(blocks)
    compiled = asyncio.run(compile_llm_filter_batches(blocks))

    config["blocks"] = compiled
    conn.execute(
        "UPDATE feeds SET config_json=? WHERE id=?",
        (json.dumps(config), feed_id),
    )
    return {
        "feed_id": feed_id,
        "feed_name": feed_name,
        "recompiled_llm_filters": llm_filter_count,
        "skipped": False,
    }


def _list_candidate_feed_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT id, config_json FROM feeds").fetchall()
    feed_ids: list[str] = []
    for feed_id, config_json in rows:
        try:
            config = json.loads(config_json) if config_json else {}
        except json.JSONDecodeError:
            continue
        blocks = config.get("blocks", [])
        if isinstance(blocks, list) and count_llm_filters(blocks) > 0:
            feed_ids.append(str(feed_id))
    return feed_ids


def main() -> None:
    parser = argparse.ArgumentParser(description="Recompile and persist batch prompts for one feed.")
    parser.add_argument("feed_id", nargs="?", default=None)
    parser.add_argument("--db", default="feed_builder_app.db")
    parser.add_argument("--all", action="store_true", help="Repair every feed that currently has llm_filter blocks.")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        target_feed_ids = _list_candidate_feed_ids(conn) if args.all else [args.feed_id or DEFAULT_FEED_ID]
        if not target_feed_ids:
            raise SystemExit("No feeds with llm_filter blocks were found")

        repaired: list[dict[str, Any]] = []
        for feed_id in target_feed_ids:
            repaired.append(_repair_feed(conn, feed_id=feed_id))
        conn.commit()

        print(
            json.dumps(
                {
                    "status": "ok",
                    "feed_count": len(repaired),
                    "feeds": repaired,
                },
                indent=2,
            )
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
