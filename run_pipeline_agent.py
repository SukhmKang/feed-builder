"""CLI entrypoint for building a feed config from a topic string."""

import argparse
import asyncio
import json
from pathlib import Path

from pipeline_agent import build_feed_config


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a feed config for a topic")
    parser.add_argument("topic", help="Topic description, for example: 'Ace Attorney game updates'")
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=1,
        help="Maximum pipeline refinement iterations",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Disable detailed pipeline agent logging",
    )
    parser.add_argument(
        "--output",
        help="Optional path to write the full run summary JSON",
    )
    return parser


def _write_output_json(path_str: str, result: dict) -> None:
    output_path = Path(path_str).expanduser()
    if output_path.parent != Path("."):
        output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


async def _main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    result = await build_feed_config(
        args.topic,
        max_iterations=args.max_iterations,
        verbose=not args.quiet,
    )
    if args.output:
        _write_output_json(args.output, result)
    print(json.dumps(result, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    asyncio.run(_main())
