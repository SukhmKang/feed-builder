"""Utility Claude Agent SDK tools for environment inspection and venv package installs."""

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool

from agent_tools.common import error, success
from pipeline_schema import deserialize_pipeline

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOTENV_PATH = PROJECT_ROOT / ".env"
VENV_PIP = PROJECT_ROOT / "venv" / "bin" / "pip"


def _read_dotenv_keys() -> list[str]:
    if not DOTENV_PATH.exists():
        return []

    keys: list[str] = []
    seen: set[str] = set()
    for raw_line in DOTENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


def _validate_package_spec(package: str) -> str:
    normalized = str(package).strip()
    if not normalized:
        raise ValueError("package must be non-empty")
    if "\n" in normalized or "\r" in normalized:
        raise ValueError("package must be a single-line string")
    return normalized


def _pip_show(package: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(VENV_PIP), "show", package],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _pip_install(package: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(VENV_PIP), "install", package],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _extract_installed_version(show_output: str) -> str | None:
    for line in show_output.splitlines():
        if line.lower().startswith("version:"):
            version = line.split(":", 1)[1].strip()
            return version or None
    return None


@tool(
    "list_env_vars",
    "List environment variable names defined in the local .env file without revealing their values.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    },
)
async def list_env_vars_tool(args: dict[str, Any]) -> dict[str, Any]:
    del args
    try:
        keys = await asyncio.to_thread(_read_dotenv_keys)
    except Exception as exc:
        return error(f"list_env_vars failed: {exc}")

    return success({"dotenv_path": str(DOTENV_PATH), "variables": keys})


@tool(
    "install_package",
    "Install a Python package into the project venv, or do nothing if it is already installed.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "package": {"type": "string"},
        },
        "required": ["package"],
    },
)
async def install_package_tool(args: dict[str, Any]) -> dict[str, Any]:
    try:
        package = _validate_package_spec(args.get("package", ""))
    except Exception as exc:
        return error(f"install_package failed: {exc}")

    if not VENV_PIP.exists():
        return error(f"install_package failed: pip not found at {VENV_PIP}")

    show_result = await asyncio.to_thread(_pip_show, package)
    if show_result.returncode == 0:
        return success(
            {
                "package": package,
                "status": "already_installed",
                "version": _extract_installed_version(show_result.stdout),
            }
        )

    install_result = await asyncio.to_thread(_pip_install, package)
    if install_result.returncode != 0:
        return error(
            "install_package failed: "
            + (install_result.stderr.strip() or install_result.stdout.strip() or "unknown pip install error")
        )

    confirm_result = await asyncio.to_thread(_pip_show, package)
    return success(
        {
            "package": package,
            "status": "installed",
            "version": _extract_installed_version(confirm_result.stdout),
        }
    )


@tool(
    "validate_pipeline_json",
    "Validate a candidate pipeline JSON array and return whether it is valid plus any validation error.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "blocks_json": {
                "type": "array",
                "items": {"type": "object"},
            }
        },
        "required": ["blocks_json"],
    },
)
async def validate_pipeline_json_tool(args: dict[str, Any]) -> dict[str, Any]:
    blocks_json = args.get("blocks_json")
    if not isinstance(blocks_json, list) or not all(isinstance(item, dict) for item in blocks_json):
        return error("validate_pipeline_json requires blocks_json to be a list of block objects")

    try:
        deserialize_pipeline(blocks_json)
    except Exception as exc:
        return success({"valid": False, "error": str(exc)})

    return success({"valid": True, "error": None})


UTILITY_TOOLS = [
    list_env_vars_tool,
    install_package_tool,
    validate_pipeline_json_tool,
]
