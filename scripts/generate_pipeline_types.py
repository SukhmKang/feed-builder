#!/usr/bin/env python3
"""Generate TypeScript types from the Pydantic pipeline schema models.

Usage:
    python scripts/generate_pipeline_types.py

Requires:
    - json-schema-to-typescript (npm): installed as a frontend devDependency
    - Run from the project root

Output:
    frontend/src/pipeline_types.generated.ts
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, get_args

from pydantic import TypeAdapter

# Ensure project root is on the path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.pipeline.schema_models import ArticleField, BlockSchema, ConditionSchema, SourceType, SwitchBranchSchema  # noqa: E402


def _literal_values(annotation: Any) -> list[str]:
    return [str(value) for value in get_args(annotation)]


def build_runtime_constants() -> str:
    article_fields = _literal_values(ArticleField)
    source_types = _literal_values(SourceType)
    return (
        "\n\n"
        f"export const PIPELINE_ARTICLE_FIELDS = {json.dumps(article_fields)} as const;\n"
        f"export const PIPELINE_SOURCE_TYPE_VALUES = {json.dumps(source_types)} as const;\n"
    )


def build_combined_schema() -> dict:
    """Build a combined JSON Schema document covering blocks, conditions, and branch."""
    block_schema = TypeAdapter(BlockSchema).json_schema()
    condition_schema = TypeAdapter(ConditionSchema).json_schema()
    branch_schema = TypeAdapter(SwitchBranchSchema).json_schema()

    # Merge $defs from all three schemas into one document, keeping $defs
    # (not "definitions") so that $ref pointers like "#/$defs/Foo" resolve.
    defs: dict = {}
    defs.update(block_schema.get("$defs", {}))
    defs.update(condition_schema.get("$defs", {}))
    defs.update(branch_schema.get("$defs", {}))

    # Wrap each top-level union in a named $def so json-schema-to-typescript
    # produces an export with the correct name.
    defs["PipelineBlock"] = {k: v for k, v in block_schema.items() if k != "$defs"}
    defs["PipelineCondition"] = {k: v for k, v in condition_schema.items() if k != "$defs"}
    defs["SwitchBranch"] = {k: v for k, v in branch_schema.items() if k != "$defs"}

    combined = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "PipelineTypes",
        "$defs": defs,
        # Reference each named type so json-schema-to-typescript includes them
        "anyOf": [
            {"$ref": "#/$defs/PipelineBlock"},
            {"$ref": "#/$defs/PipelineCondition"},
            {"$ref": "#/$defs/SwitchBranch"},
        ],
    }
    return combined


def run_codegen(schema: dict, output_path: Path) -> None:
    npx = project_root / "frontend" / "node_modules" / ".bin" / "npx"
    npx_cmd = str(npx) if npx.exists() else "npx"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix="pipeline_schema_"
    ) as tmp:
        json.dump(schema, tmp, indent=2)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            [
                npx_cmd,
                "json-schema-to-typescript",
                tmp_path,
                "--no-additionalProperties",
                "--unreachableDefinitions",
            ],
            capture_output=True,
            text=True,
            cwd=str(project_root / "frontend"),
        )
        if result.returncode != 0:
            print("json-schema-to-typescript error:", result.stderr, file=sys.stderr)
            sys.exit(1)
        ts_output = result.stdout
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    # Prepend a generation header
    header = "// AUTO-GENERATED — do not edit by hand.\n// Run: python scripts/generate_pipeline_types.py\n\n"
    output_path.write_text(header + ts_output + build_runtime_constants())
    print(f"Generated: {output_path.relative_to(project_root)}")


def main() -> None:
    output_path = project_root / "frontend" / "src" / "pipeline_types.generated.ts"
    schema = build_combined_schema()
    run_codegen(schema, output_path)


if __name__ == "__main__":
    main()
