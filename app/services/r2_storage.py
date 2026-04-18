"""Cloudflare R2 storage helpers (S3-compatible via boto3)."""

from __future__ import annotations

import logging
import os

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)

_PRESIGNED_URL_EXPIRY = 3600  # 1 hour


def _client():
    account_id = os.getenv("R2_ACCOUNT_ID", "")
    access_key = os.getenv("R2_ACCESS_KEY_ID", "")
    secret_key = os.getenv("R2_SECRET_ACCESS_KEY", "")
    if not account_id:
        raise RuntimeError("R2_ACCOUNT_ID is not set")
    if not access_key or not secret_key:
        raise RuntimeError("R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY are not set")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def _bucket() -> str:
    return os.getenv("R2_BUCKET_NAME", "pdf-reports")


def upload_pdf(key: str, data: bytes) -> None:
    """Upload PDF bytes to R2 under the given key."""
    _client().put_object(
        Bucket=_bucket(),
        Key=key,
        Body=data,
        ContentType="application/pdf",
    )
    logger.info("r2_storage: uploaded %s (%d bytes)", key, len(data))


def presigned_download_url(key: str) -> str:
    """Return a time-limited presigned URL for downloading an object."""
    url: str = _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": _bucket(), "Key": key},
        ExpiresIn=_PRESIGNED_URL_EXPIRY,
    )
    return url


def delete_object(key: str) -> None:
    """Delete an object from R2."""
    _client().delete_object(Bucket=_bucket(), Key=key)
    logger.info("r2_storage: deleted %s", key)
