"""Wraps run_pipeline_agent.py as a subprocess to build a feed config from a topic."""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent


async def build_feed(topic: str, max_iterations: int = 2) -> dict:
    """Run run_pipeline_agent.py and return the parsed full result dict.

    Raises RuntimeError if the subprocess exits non-zero.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(ROOT / "run_pipeline_agent.py"),
        topic,
        "--max-iterations",
        str(max_iterations),
        "--quiet",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(ROOT),
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        error_text = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Pipeline agent failed (exit {proc.returncode}): {error_text[-2000:]}")

    raw = stdout.decode("utf-8", errors="replace").strip()
    return json.loads(raw)
