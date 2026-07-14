"""One-time (resumable) fleet backfill of inverter model info + images.

For every device in saj_device:
  1. pull baseInverterDetail from SAJ (model, rated kW, phase, firmware, image)
  2. mirror the model image from SAJ cloud into our R2 bucket (deduped by filename,
     downloaded once — images are per-model, shared across many devices)
  3. store model info + OUR R2 image URL on saj_device

So the app reads model + a fast R2/CDN image URL straight from our DB, never touching
SAJ cloud at view time.

Resumable: skips devices already pointing at our R2 image, unless --force.
Throttled to stay under the SAJ rate alarm.

Env:
  SAJ_USER / SAJ_PASS
  DATABASE_URL  or  PG_PROXY_TOKEN         (prod DB)
  R2_ENDPOINT R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_BUCKET R2_PUBLIC_BASE
  R2_PREFIX=saj-inverter                    (key prefix in the bucket)
  SAJ_REQ_INTERVAL=1.0                       (throttle)
  LIMIT=                                     (cap devices, for testing)

Run:  python backfill_device_info.py [--force]
"""
from __future__ import annotations

import os
import sys
import time
import random

import requests
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

import pg
from saj_api import SajClient

REQ_INTERVAL = float(os.environ.get("SAJ_REQ_INTERVAL", "1.0"))
JITTER = float(os.environ.get("SAJ_REQ_JITTER", "0.3"))
LIMIT = int(os.environ["LIMIT"]) if os.environ.get("LIMIT") else None
FORCE = "--force" in sys.argv

R2_PREFIX = os.environ.get("R2_PREFIX", "saj-inverter").strip("/")
R2_BUCKET = os.environ["R2_BUCKET"]
R2_PUBLIC_BASE = os.environ["R2_PUBLIC_BASE"].rstrip("/")

_s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["R2_ENDPOINT"],
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    region_name="auto",
    config=Config(signature_version="s3v4"),
)

_img_cache: dict[str, str] = {}  # saj_url -> our public R2 url


def _f(v):
    try:
        if v in (None, "", "--"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def mirror_image(saj_url: str | None) -> str | None:
    """Ensure the SAJ image is in R2; return our public URL. Deduped + idempotent."""
    if not saj_url:
        return None
    if saj_url in _img_cache:
        return _img_cache[saj_url]
    filename = saj_url.split("/")[-1].split("?")[0] or "unknown.png"
    key = f"{R2_PREFIX}/{filename}"
    our_url = f"{R2_PUBLIC_BASE}/{key}"
    try:                                    # already mirrored?
        _s3.head_object(Bucket=R2_BUCKET, Key=key)
        _img_cache[saj_url] = our_url
        return our_url
    except ClientError:
        pass
    resp = requests.get(saj_url, timeout=30)
    resp.raise_for_status()
    _s3.put_object(Bucket=R2_BUCKET, Key=key, Body=resp.content,
                   ContentType=resp.headers.get("Content-Type", "image/png"),
                   CacheControl="public, max-age=31536000, immutable")
    print(f"  [r2] uploaded {key} ({len(resp.content)} bytes)", flush=True)
    _img_cache[saj_url] = our_url
    return our_url


def _devices() -> list[str]:
    r = pg.run("select device_sn from saj_device order by device_sn")
    return [row["device_sn"] for row in r.get("rows", [])]


def _already_done(sn: str) -> bool:
    r = pg.run("select image_url, model from saj_device where device_sn=$1", [sn])
    rows = r.get("rows") or []
    if not rows:
        return False
    iu = rows[0].get("image_url") or ""
    return bool(rows[0].get("model")) and iu.startswith(R2_PUBLIC_BASE)


def main():
    user, pw = os.environ.get("SAJ_USER"), os.environ.get("SAJ_PASS")
    if not (user and pw):
        raise SystemExit("SAJ_USER / SAJ_PASS not set")
    client = SajClient(username=user, password=pw)

    sns = _devices()
    if LIMIT:
        sns = sns[:LIMIT]
    total = len(sns)
    print(f"[backfill] backend={pg.backend()} devices={total} force={FORCE}", flush=True)

    ok = skip = err = 0
    t0 = time.time()
    for i, sn in enumerate(sns, 1):
        if not FORCE and _already_done(sn):
            skip += 1
            continue
        try:
            b = client.inverter_base(sn)
            our_img = mirror_image(b.get("inverterPic"))
            pg.run(
                "update saj_device set model=$2, rated_power_kw=$3, phase_name=$4, "
                "firmware=$5, image_url=$6, updated_at=now() where device_sn=$1",
                [sn, b.get("inverterModel"), _f(b.get("ratedPower")),
                 b.get("phaseName"), b.get("displayFw"), our_img],
            )
            ok += 1
            if ok <= 5 or ok % 50 == 0:
                print(f"  [{i}/{total}] {sn} -> {b.get('inverterModel')} | {our_img}", flush=True)
        except Exception as e:  # noqa: BLE001 — keep the sweep alive
            err += 1
            print(f"  [{i}/{total}] {sn} FAIL {e}", flush=True)
        time.sleep(REQ_INTERVAL + random.uniform(0, JITTER))

    print(f"[backfill] DONE ok={ok} skip={skip} err={err} unique_images={len(_img_cache)} "
          f"in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
