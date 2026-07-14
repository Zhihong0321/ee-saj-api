"""Nightly full-fleet sync — run at 23:00 MYT (15:00 UTC) via Railway Cron.

Fetches today's complete 5-min feed for EVERY device into saj_reading, so the
day's curve is guaranteed gap-free once the sun is down. Always a full pull (no
freshness gate). Rate-throttled to stay under the SAJ rate alarm; per-device
errors are logged and skipped, never abort the run. Exits when done.

Device list comes from the saj_device catalog; if that's empty it falls back to
enumerating the fleet live from the portal.

Env:
  SAJ_USER / SAJ_PASS        SAJ monitoring account (required)
  DATABASE_URL               prod Postgres (same DB the web service + app use)
  SAJ_REQ_INTERVAL=1.0       seconds between requests (throttle)
  SAJ_REQ_JITTER=0.3         added random jitter
  SYNC_DAYS=1                days back to pull (1 = today only)
  SYNC_LIMIT=                cap device count (testing)

Run:  python sync_all.py
"""
from __future__ import annotations

import os
import time
import random

import pg
import fetcher
from saj_api import SajClient

REQ_INTERVAL = float(os.environ.get("SAJ_REQ_INTERVAL", "1.0"))
JITTER = float(os.environ.get("SAJ_REQ_JITTER", "0.3"))
DAYS = int(os.environ.get("SYNC_DAYS", "1"))
LIMIT = int(os.environ["SYNC_LIMIT"]) if os.environ.get("SYNC_LIMIT") else None


def _device_sns(client: SajClient) -> list[str]:
    r = pg.run("select device_sn from saj_device order by device_sn")
    sns = [row["device_sn"] for row in r.get("rows", [])]
    if sns:
        return sns
    print("[sync-all] saj_device empty — enumerating fleet live", flush=True)
    return [sn for _, _, sn in client.iter_all_devices()]


def main():
    user = os.environ.get("SAJ_USER")
    pw = os.environ.get("SAJ_PASS")
    if not (user and pw):
        raise SystemExit("SAJ_USER / SAJ_PASS not set")

    client = SajClient(username=user, password=pw)
    sns = _device_sns(client)
    if LIMIT:
        sns = sns[:LIMIT]
    total = len(sns)
    print(f"[sync-all] backend={pg.backend()} devices={total} days={DAYS} "
          f"interval={REQ_INTERVAL}s", flush=True)

    ok = err = rows = 0
    t0 = time.time()
    for i, sn in enumerate(sns, 1):
        try:
            res = fetcher.fetch_device(client, sn, days=DAYS)  # no gate -> full pull
            rows += res["rows_written"]
            ok += 1
        except Exception as e:  # noqa: BLE001 — keep the sweep alive
            err += 1
            print(f"[sync-all] {sn} FAIL {e}", flush=True)
        if i % 50 == 0 or i == total:
            print(f"[sync-all] {i}/{total} ok={ok} err={err} rows={rows} "
                  f"({time.time() - t0:.0f}s)", flush=True)
        time.sleep(REQ_INTERVAL + random.uniform(0, JITTER))

    print(f"[sync-all] DONE ok={ok} err={err} rows={rows} in "
          f"{time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
