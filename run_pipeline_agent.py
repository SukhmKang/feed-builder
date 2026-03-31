"""CLI entrypoint for building a feed config from a topic string."""

import argparse
import asyncio
import json
from pathlib import Path

from pipeline_agent import build_feed_config, build_feed_config_from_sources, build_sources_for_topic


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a feed config for a topic")
    parser.add_argument("topic", help="Topic description, for example: 'Ace Attorney game updates'")
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=2,
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
    parser.add_argument(
        "--sources-output",
        help="Optional path to write the intermediate source-generation JSON bundle",
    )
    parser.add_argument(
        "--sources-input",
        help="Optional path to an existing source-generation JSON bundle to reuse instead of rerunning source discovery",
    )
    return parser


def _write_output_json(path_str: str, result: dict) -> None:
    output_path = Path(path_str).expanduser()
    if output_path.parent != Path("."):
        output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _derive_sources_output_path(output_path_str: str) -> str:
    output_path = Path(output_path_str).expanduser()
    return str(output_path.with_suffix(".sources.json"))


async def _main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    sources_output_path = args.sources_output
    if not sources_output_path and args.output and not args.sources_input:
        sources_output_path = _derive_sources_output_path(args.output)

    if args.sources_input:
        source_generation = json.loads(Path(args.sources_input).expanduser().read_text(encoding="utf-8"))
    else:
        source_generation = await build_sources_for_topic(
            args.topic,
            verbose=not args.quiet,
        )
        if sources_output_path:
            _write_output_json(sources_output_path, source_generation)

    result = await build_feed_config_from_sources(
        args.topic,
        source_generation=source_generation,
        max_iterations=args.max_iterations,
        verbose=not args.quiet,
    )

    if sources_output_path:
        result = dict(result)
        result["source_generation_path"] = str(Path(sources_output_path).expanduser())
    if args.sources_input:
        result = dict(result)
        result["source_generation_path"] = str(Path(args.sources_input).expanduser())
    if args.output:
        _write_output_json(args.output, result)
    print(json.dumps(result, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    asyncio.run(_main())
