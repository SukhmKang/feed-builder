"""Dispatch feed build jobs to the worker service."""

from app.worker.client import dispatch_build_feed


async def build_feed(feed_id: str, topic: str, max_iterations: int = 2) -> None:
    """Dispatch a feed build job to the worker service.

    The worker runs the pipeline agent and writes results directly to the shared DB.
    Feed status transitions (building → ready/error) are handled by the worker.
    """
    await dispatch_build_feed(feed_id, topic, max_iterations)
