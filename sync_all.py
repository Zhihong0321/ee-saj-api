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

Fast mode: pass --customer or --plant to sync just that one account instead of
the fleet — seconds, instead of the ~20 minute sweep. See fast_sync.py.

    python sync_all.py --customer "Ah Seng"
    python sync_all.py --plant "Taman Molek" --days 7
"""
from __future__ import annotations

import argparse
import os
import time
import random

import pg
import fetcher
import fast_sync
import accounts
from saj_api import SajClient


def _make_client() -> SajClient:
    """Portal client for the primary DB account, with an env-var fallback so an
    older deploy (or a fresh DB) still runs."""
    acct = accounts.get_primary()
    if acct:
        return SajClient(username=acct["username"], password=acct["password"],
                         org_code=acct.get("org_code") or "OAhz")
    user, pw = os.environ.get("SAJ_USER"), os.environ.get("SAJ_PASS")
    if user and pw:
        return SajClient(username=user, password=pw)
    raise SystemExit("no primary SAJ account configured (add one at /accounts)")

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


def _parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--customer", help="fast mode: sync only this customer")
    target.add_argument("--plant", help="fast mode: sync only this plant")
    parser.add_argument("--days", type=int, default=DAYS,
                        help="days back to pull (default: SYNC_DAYS)")
    parser.add_argument("--limit", type=int, default=LIMIT,
                        help="cap device count (default: SYNC_LIMIT)")
    return parser.parse_args()


def main():
    args = _parse_args()
    client = _make_client()

    if args.customer or args.plant:
        try:
            summary = fast_sync.run(client, customer=args.customer,
                                    plant=args.plant, days=args.days)
        except fast_sync.TargetAmbiguous as e:
            raise SystemExit(f"[fast] ambiguous: {e}")
        except fast_sync.TargetNotFound as e:
            raise SystemExit(f"[fast] not found: {e}")
        return 1 if summary["err"] else 0

    sns = _device_sns(client)
    if args.limit:
        sns = sns[:args.limit]
    total = len(sns)
    print(f"[sync-all] backend={pg.backend()} devices={total} days={args.days} "
          f"interval={REQ_INTERVAL}s", flush=True)

    ok = err = rows = 0
    t0 = time.time()
    for i, sn in enumerate(sns, 1):
        try:
            res = fetcher.fetch_device(client, sn, days=args.days)  # no gate -> full pull
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
    return 1 if err else 0


if __name__ == "__main__":
    raise SystemExit(main())
