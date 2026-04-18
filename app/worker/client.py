"""HTTP dispatch helpers — API server calls these to hand off jobs to the worker service."""

import os
from datetime import datetime
from typing import Any

import httpx

WORKER_URL = os.environ.get("WORKER_URL", "http://localhost:8001")
_TIMEOUT = 10.0


async def _post(path: str, body: dict[str, Any]) -> None:
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{WORKER_URL}{path}", json=body, timeout=_TIMEOUT)
        resp.raise_for_status()


async def dispatch_build_feed(feed_id: str, topic: str, max_iterations: int = 2) -> None:
    await _post("/build-feed", {"feed_id": feed_id, "topic": topic, "max_iterations": max_iterations})


async def dispatch_audit(
    *,
    feed_id: str,
    start: datetime,
    end: datetime,
    enable_replay: bool,
    enable_discovery: bool,
    user_context: str | None = None,
) -> None:
    await _post(
        "/run-audit",
        {
            "feed_id": feed_id,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "enable_replay": enable_replay,
            "enable_discovery": enable_discovery,
            "user_context": user_context,
        },
    )


async def dispatch_replay(
    feed_id: str,
    version_id: str,
    lookback_days: int | None,
) -> None:
    await _post("/replay", {"feed_id": feed_id, "version_id": version_id, "lookback_days": lookback_days})


async def dispatch_poll(feed_id: str, lookback_hours: int | None) -> None:
    await _post("/poll", {"feed_id": feed_id, "lookback_hours": lookback_hours})
