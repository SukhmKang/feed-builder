"""Wraps app/agents/run_pipeline_agent.py as a subprocess to build a feed config from a topic."""

import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
logger = logging.getLogger(__name__)


async def build_feed(topic: str, max_iterations: int = 2) -> dict:
    """Run app/agents/run_pipeline_agent.py and return the parsed full result dict.

    Raises RuntimeError if the subprocess exits non-zero.
    """
    env = {**os.environ, "PYTHONPATH": str(ROOT)}

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        output_path = tmp.name

    try:
        logger.info("build_feed subprocess start topic=%r max_iterations=%s", topic, max_iterations)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(ROOT / "app" / "agents" / "run_pipeline_agent.py"),
            topic,
            "--max-iterations",
            str(max_iterations),
            "--output",
            output_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(ROOT),
            env=env,
        )

        # Stream stdout (agent logs) to uvicorn in real-time
        async def _forward_stdout() -> None:
            assert proc.stdout is not None
            async for line in proc.stdout:
                print(line.decode("utf-8", errors="replace"), end="", flush=True)

        async def _collect_stderr() -> bytes:
            assert proc.stderr is not None
            chunks = []
            async for chunk in proc.stderr:
                chunks.append(chunk)
            return b"".join(chunks)

        stdout_task = asyncio.create_task(_forward_stdout())
        stderr_task = asyncio.create_task(_collect_stderr())
        await proc.wait()
        await stdout_task
        stderr_bytes = await stderr_task
        logger.info("build_feed subprocess exited returncode=%s topic=%r", proc.returncode, topic)

        if proc.returncode != 0:
            error_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Pipeline agent failed (exit {proc.returncode}): {error_text}")

        result = json.loads(Path(output_path).read_text(encoding="utf-8"))
        logger.info(
            "build_feed subprocess output parsed topic=%r merged_sources=%s blocks=%s satisfied=%s iterations=%s",
            topic,
            len(result.get("merged_sources", []) or []),
            len(result.get("blocks_json", []) or []),
            result.get("satisfied"),
            result.get("iterations"),
        )
        return result
    finally:
        Path(output_path).unlink(missing_ok=True)
