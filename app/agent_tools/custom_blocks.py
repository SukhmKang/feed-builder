"""Claude Agent SDK tools for managing user-defined custom blocks."""

import importlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool

from app.agent_tools.common import error, success
from app.pipeline import CustomBlock
from app.pipeline.core import cosine_similarity, embed_text

CUSTOM_BLOCKS_DIR = Path(__file__).resolve().parent.parent / "custom_blocks"
CUSTOM_BLOCK_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_custom_block_name(name: str) -> str:
    normalized = str(name).strip()
    if not normalized:
        raise ValueError("custom block name must be non-empty")
    if not CUSTOM_BLOCK_NAME_RE.fullmatch(normalized):
        raise ValueError(
            "custom block name must be a valid Python module name using only letters, numbers, and underscores"
        )
    if normalized == "__init__":
        raise ValueError("__init__ is reserved and cannot be used as a custom block name")
    return normalized


def _custom_block_path(name: str) -> Path:
    return CUSTOM_BLOCKS_DIR / f"{name}.py"


def _registry_path() -> Path:
    return CUSTOM_BLOCKS_DIR / "_registry.db"


def _legacy_registry_path() -> Path:
    return CUSTOM_BLOCKS_DIR / "_registry.json"


def _connect_registry() -> sqlite3.Connection:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS custom_blocks (
            name TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            created_at TEXT,
            embedding TEXT
        )
        """
    )
    return connection


def _migrate_legacy_registry_if_needed() -> None:
    legacy_path = _legacy_registry_path()
    if not legacy_path.exists():
        return

    try:
        raw = json.loads(legacy_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(raw, dict) or not raw:
        return

    with _connect_registry() as connection:
        existing_count = connection.execute("SELECT COUNT(*) FROM custom_blocks").fetchone()[0]
        if existing_count:
            return

        for name, metadata in raw.items():
            if not isinstance(metadata, dict):
                continue
            connection.execute(
                """
                INSERT OR REPLACE INTO custom_blocks (name, title, description, created_at, embedding)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(name).strip(),
                    str(metadata.get("title", "")).strip() or None,
                    str(metadata.get("description", "")).strip() or None,
                    str(metadata.get("created_at", "")).strip() or None,
                    json.dumps(metadata.get("embedding", []), ensure_ascii=True),
                ),
            )
        connection.commit()


def _read_registry() -> dict[str, Any]:
    _migrate_legacy_registry_if_needed()
    try:
        with _connect_registry() as connection:
            rows = connection.execute(
                "SELECT name, title, description, created_at, embedding FROM custom_blocks"
            ).fetchall()
    except Exception:
        return {}

    registry: dict[str, Any] = {}
    for name, title, description, created_at, embedding_raw in rows:
        try:
            embedding = json.loads(embedding_raw) if embedding_raw else []
        except Exception:
            embedding = []
        registry[str(name)] = {
            "title": title,
            "description": description,
            "created_at": created_at,
            "embedding": embedding,
        }
    return registry


def _write_registry(data: dict[str, Any]) -> None:
    _migrate_legacy_registry_if_needed()
    with _connect_registry() as connection:
        connection.execute("DELETE FROM custom_blocks")
        for name, metadata in data.items():
            if not isinstance(metadata, dict):
                continue
            connection.execute(
                """
                INSERT OR REPLACE INTO custom_blocks (name, title, description, created_at, embedding)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(name).strip(),
                    str(metadata.get("title", "")).strip() or None,
                    str(metadata.get("description", "")).strip() or None,
                    str(metadata.get("created_at", "")).strip() or None,
                    json.dumps(metadata.get("embedding", []), ensure_ascii=True),
                ),
            )
        connection.commit()


def _reload_custom_block_module(name: str) -> None:
    module_name = f"app.custom_blocks.{name}"
    importlib.invalidate_caches()
    sys.modules.pop(module_name, None)


@tool(
    "list_custom_blocks",
    "List available custom block module names from the custom_blocks package.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    },
)
async def list_custom_blocks_tool(args: dict[str, Any]) -> dict[str, Any]:
    del args
    registry = _read_registry()
    names = sorted(
        path.stem
        for path in CUSTOM_BLOCKS_DIR.glob("*.py")
        if path.is_file() and path.stem != "__init__"
    )
    return success(
        {
            "custom_blocks": [
                {
                    "name": name,
                    "title": (
                        str(registry.get(name, {}).get("title")).strip()
                        if isinstance(registry.get(name), dict) and registry.get(name, {}).get("title") is not None
                        else None
                    ),
                    "description": (
                        str(registry.get(name, {}).get("description")).strip()
                        if isinstance(registry.get(name), dict) and registry.get(name, {}).get("description") is not None
                        else None
                    ),
                }
                for name in names
            ]
        }
    )


@tool(
    "read_custom_block",
    "Read the source code of a custom block module from app/custom_blocks/.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string"},
        },
        "required": ["name"],
    },
)
async def read_custom_block_tool(args: dict[str, Any]) -> dict[str, Any]:
    try:
        name = _validate_custom_block_name(args.get("name", ""))
        path = _custom_block_path(name)
        if not path.exists():
            return error(f"custom block does not exist: {name}")
        code = path.read_text(encoding="utf-8")
    except Exception as exc:
        return error(f"read_custom_block failed: {exc}")

    return success({"name": name, "path": str(path), "code": code})


@tool(
    "get_custom_block_docs",
    "Get the required contract and example structure for authoring a custom pipeline block. Call this if you are about to write a new custom block and need the exact interface.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    },
)
async def get_custom_block_docs_tool(args: dict[str, Any]) -> dict[str, Any]:
    del args
    example_code = '''from dataclasses import dataclass
from typing import Any

from app.pipeline.core import BlockResult, copy_article


@dataclass(slots=True)
class ExampleBlock:
    keyword: str

    async def run(self, article: dict[str, Any]) -> BlockResult:
        working_article = copy_article(article)
        text = str(working_article.get("title", "")) + " " + str(working_article.get("content", ""))
        passed = self.keyword.lower() in text.lower()
        return {
            "passed": passed,
            "article": working_article,
            "reason": "Matched keyword" if passed else "Keyword not found",
        }
'''
    return success(
        {
            "summary": (
                "A custom block module should define one or more importable classes that can be "
                "instantiated by the pipeline loader. Each custom block class must expose "
                "an async run(article) method returning a BlockResult dict."
            ),
            "requirements": [
                "Use normal top-level Python imports; do not hide imports inside functions.",
                "Define a class that can be imported from the module.",
                "Implement async def run(self, article: dict[str, Any]) -> BlockResult.",
                "Return a dict with keys: passed (bool), article (dict), reason (str).",
                "Prefer copying the article before mutation, e.g. with copy_article(article).",
                "Keep the block reusable and avoid hardcoding the current topic unless truly necessary.",
            ],
            "example_code": example_code,
        }
    )


@tool(
    "write_custom_block",
    "Create or overwrite a custom block module in app/custom_blocks/.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "code": {"type": "string"},
        },
        "required": ["name", "title", "description", "code"],
    },
)
async def write_custom_block_tool(args: dict[str, Any]) -> dict[str, Any]:
    try:
        name = _validate_custom_block_name(args.get("name", ""))
        title = str(args.get("title", "")).strip()
        description = str(args.get("description", "")).strip()
        code = str(args.get("code", ""))
        if not title:
            return error("write_custom_block requires non-empty title")
        if not description:
            return error("write_custom_block requires non-empty description")
        if not code.strip():
            return error("write_custom_block requires non-empty code")
        path = _custom_block_path(name)
        path.write_text(code, encoding="utf-8")
        embedding = await embed_text(f"{title}. {description}", model="text-embedding-3-small")
        registry = _read_registry()
        registry[name] = {
            "title": title,
            "description": description,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "embedding": embedding,
        }
        _write_registry(registry)
        _reload_custom_block_module(name)
    except Exception as exc:
        return error(f"write_custom_block failed: {exc}")

    return success({"name": name, "title": title, "description": description, "path": str(path), "written": True})


@tool(
    "delete_custom_block",
    "Delete a custom block module from app/custom_blocks/.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string"},
        },
        "required": ["name"],
    },
)
async def delete_custom_block_tool(args: dict[str, Any]) -> dict[str, Any]:
    try:
        name = _validate_custom_block_name(args.get("name", ""))
        path = _custom_block_path(name)
        if not path.exists():
            return error(f"custom block does not exist: {name}")
        path.unlink()
        registry = _read_registry()
        if name in registry:
            registry.pop(name, None)
            _write_registry(registry)
        _reload_custom_block_module(name)
    except Exception as exc:
        return error(f"delete_custom_block failed: {exc}")

    return success({"name": name, "path": str(path), "deleted": True})


@tool(
    "test_custom_block",
    "Run a custom block against sample article dicts and return the results.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string"},
            "sample_articles": {
                "type": "array",
                "items": {"type": "object"},
                "minItems": 1,
            },
        },
        "required": ["name", "sample_articles"],
    },
)
async def test_custom_block_tool(args: dict[str, Any]) -> dict[str, Any]:
    sample_articles = args.get("sample_articles")
    if not isinstance(sample_articles, list) or not sample_articles or not all(
        isinstance(item, dict) for item in sample_articles
    ):
        return error("test_custom_block requires sample_articles to be a non-empty list of article objects")

    try:
        name = _validate_custom_block_name(args.get("name", ""))
        _reload_custom_block_module(name)
        block = CustomBlock(name)
        results: list[dict[str, Any]] = []
        for index, article in enumerate(sample_articles):
            result = await block.run(article)
            results.append(
                {
                    "index": index,
                    "passed": bool(result.get("passed", False)),
                    "reason": str(result.get("reason", "")).strip(),
                    "article": result.get("article", {}),
                }
            )
    except Exception as exc:
        return error(f"test_custom_block failed: {exc}")

    return success(
        {
            "name": name,
            "sample_count": len(sample_articles),
            "results": results,
        }
    )


@tool(
    "search_custom_blocks",
    "Semantic search across existing custom blocks by title and description. Call this before creating a new custom block to find reusable ones.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    },
)
async def search_custom_blocks_tool(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    if not query:
        return error("search_custom_blocks requires a non-empty query")

    try:
        registry = _read_registry()
        entries: list[tuple[str, dict[str, Any]]] = [
            (name, metadata)
            for name, metadata in registry.items()
            if isinstance(metadata, dict)
        ]

        results: list[dict[str, Any]] = []
        embedded_entries = [
            (name, metadata)
            for name, metadata in entries
            if isinstance(metadata.get("embedding"), list) and metadata.get("embedding")
        ]

        if embedded_entries:
            query_embedding = await embed_text(query, model="text-embedding-3-small")
            for name, metadata in embedded_entries:
                raw_embedding = metadata.get("embedding", [])
                if not isinstance(raw_embedding, list) or not all(isinstance(value, (int, float)) for value in raw_embedding):
                    continue
                similarity = cosine_similarity(query_embedding, [float(value) for value in raw_embedding])
                if similarity < 0.5:
                    continue
                results.append(
                    {
                        "name": name,
                        "title": str(metadata.get("title", "")).strip(),
                        "description": str(metadata.get("description", "")).strip(),
                        "similarity": round(float(similarity), 3),
                    }
                )
            results.sort(key=lambda item: item["similarity"], reverse=True)
            return success({"query": query, "results": results})

        lowered_query = query.lower()
        for name, metadata in entries:
            title = str(metadata.get("title", "")).strip()
            description = str(metadata.get("description", "")).strip()
            haystack = f"{title}\n{description}".lower()
            if lowered_query not in haystack:
                continue
            results.append(
                {
                    "name": name,
                    "title": title,
                    "description": description,
                    "similarity": None,
                }
            )
    except Exception as exc:
        return error(f"search_custom_blocks failed: {exc}")

    return success({"query": query, "results": results})


CUSTOM_BLOCK_TOOLS = [
    list_custom_blocks_tool,
    read_custom_block_tool,
    get_custom_block_docs_tool,
    write_custom_block_tool,
    delete_custom_block_tool,
    test_custom_block_tool,
    search_custom_blocks_tool,
]
