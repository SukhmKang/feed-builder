"""VAPID key management and Web Push notification sending."""

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid

KEYS_FILE = Path(__file__).parent.parent.parent / ".vapid_keys.json"


def _load_or_generate_keys() -> dict[str, str]:
    if KEYS_FILE.exists():
        return json.loads(KEYS_FILE.read_text())

    v = Vapid()
    v.generate_keys()

    private_pem = v.private_pem().decode("utf-8")

    raw_public = v.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public_b64 = base64.urlsafe_b64encode(raw_public).decode("utf-8").rstrip("=")

    keys = {"private_pem": private_pem, "public_b64": public_b64}
    KEYS_FILE.write_text(json.dumps(keys))
    return keys


_keys: dict[str, str] | None = None


def get_public_key() -> str:
    global _keys
    if _keys is None:
        _keys = _load_or_generate_keys()
    return _keys["public_b64"]


def _send_push_sync(subscription: dict[str, Any], payload: dict[str, Any]) -> None:
    from pywebpush import WebPushException, webpush

    global _keys
    if _keys is None:
        _keys = _load_or_generate_keys()

    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps(payload),
            vapid_private_key=_keys["private_pem"],
            vapid_claims={"sub": "mailto:feedbuilder@localhost"},
        )
    except WebPushException as exc:
        raise RuntimeError(f"Push failed: {exc}") from exc


async def send_push(subscription: dict[str, Any], payload: dict[str, Any]) -> None:
    """Send a Web Push notification. Non-blocking — wraps the sync call in a thread."""
    await asyncio.to_thread(_send_push_sync, subscription, payload)
