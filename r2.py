"""Mirror SAJ-cloud inverter images into our Cloudflare R2 bucket.

Images are per-model (many devices share one), so uploads are deduped by filename
and cached in-process. Fully optional: if the R2_* env vars aren't set, mirror_image
returns None and the caller falls back to the original SAJ URL — the service still
works, images just stay on SAJ cloud.

Env: R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET,
     R2_PUBLIC_BASE, R2_PREFIX (default 'saj-inverter').
"""
from __future__ import annotations

import os
import threading

_lock = threading.Lock()
_client = None
_cache: dict[str, str] = {}  # saj_url -> our public R2 url


def enabled() -> bool:
    return all(os.environ.get(k) for k in
               ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                "R2_BUCKET", "R2_PUBLIC_BASE"))


def _get_client():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config
        _client = boto3.client(
            "s3", endpoint_url=os.environ["R2_ENDPOINT"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto", config=Config(signature_version="s3v4"))
    return _client


def mirror_image(saj_url: str | None) -> str | None:
    """Return our public R2 URL for the image, uploading it once if needed.

    Returns None if R2 is unconfigured or the mirror fails (caller falls back to
    the SAJ URL). Never raises.
    """
    if not saj_url or not enabled():
        return None
    if saj_url in _cache:
        return _cache[saj_url]
    try:
        import requests
        from botocore.exceptions import ClientError
        bucket = os.environ["R2_BUCKET"]
        prefix = os.environ.get("R2_PREFIX", "saj-inverter").strip("/")
        base = os.environ["R2_PUBLIC_BASE"].rstrip("/")
        fname = saj_url.split("/")[-1].split("?")[0] or "unknown.png"
        key = f"{prefix}/{fname}"
        our_url = f"{base}/{key}"
        s3 = _get_client()
        with _lock:
            try:
                s3.head_object(Bucket=bucket, Key=key)          # already mirrored
            except ClientError:
                resp = requests.get(saj_url, timeout=30)
                resp.raise_for_status()
                s3.put_object(Bucket=bucket, Key=key, Body=resp.content,
                              ContentType=resp.headers.get("Content-Type", "image/png"),
                              CacheControl="public, max-age=31536000, immutable")
                print(f"[r2] mirrored {key} ({len(resp.content)} bytes)", flush=True)
        _cache[saj_url] = our_url
        return our_url
    except Exception as e:  # noqa: BLE001 — mirroring is best-effort
        print(f"[r2] mirror failed for {saj_url}: {e}", flush=True)
        return None
