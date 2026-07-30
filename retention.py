"""Retention policy: keep 5-min detail for the recent window, daily totals forever.

`saj_reading` is the 5-min feed and it is big — a full history copy put ~23M
rows / 4 GB in it. The client app needs intraday curve shape only for the
recent window; for older months a per-day kWh number is enough. So:

  1. roll every device-day up into `saj_daily_energy` (tiny: one row per
     device-day instead of ~150), then
  2. delete `saj_reading` rows older than the capture policy floor.

The prune is guarded: a day is only deleted once its rollup row exists, so a
failed or partial rollup can never silently destroy detail. Both steps are
idempotent, so running this nightly is safe.

Work is chunked by MYT day and driven off the `ts` index, so no single
statement locks the table for long.
"""
from __future__ import annotations

import datetime as dt

import pg
import backfill

# A MYT day [D 00:00, D+1 00:00) in +08:00, so the delete can use the ts index.
_DAY_START = "{d} 00:00:00+08"


def _db(sql, params=None):
    r = pg.run(sql, params)
    if isinstance(r, dict) and "error" in r:
        raise RuntimeError(f"db: {r}")
    return r


def ensure_schema():
    _db("""create table if not exists saj_daily_energy (
             device_sn  text not null,
             day        date not null,
             kwh        numeric,
             peak_w     numeric,
             samples    int,
             updated_at timestamptz not null default now(),
             primary key (device_sn, day))""")
    _db("create index if not exists saj_daily_energy_day_idx on saj_daily_energy (day)")
    # The prune filters on ts alone; saj_reading's PK is (device_sn, ts), which
    # cannot serve that, so give ts its own index.
    _db("create index if not exists saj_reading_ts_idx on saj_reading (ts)")


def _bounds() -> tuple[dt.date | None, dt.date | None]:
    rows = _db("select min(ts) as lo, max(ts) as hi from saj_reading").get("rows") or []
    if not rows or not rows[0]["lo"]:
        return None, None
    lo, hi = rows[0]["lo"], rows[0]["hi"]
    to_myt = lambda t: (t + dt.timedelta(hours=8)).date()  # noqa: E731
    return to_myt(lo), to_myt(hi)


def rollup_day(day: dt.date) -> int:
    """Upsert one MYT day of per-device daily totals. Returns device-days written."""
    nxt = day + dt.timedelta(days=1)
    r = _db(
        "insert into saj_daily_energy (device_sn, day, kwh, peak_w, samples, updated_at) "
        "select device_sn, $1::date, max(coalesce(today_kwh,0)), "
        "       max(coalesce(ac_power_w,0)), count(*), now() "
        "from saj_reading where ts >= $2::timestamptz and ts < $3::timestamptz "
        "group by device_sn "
        "on conflict (device_sn, day) do update set kwh=excluded.kwh, "
        "peak_w=excluded.peak_w, samples=excluded.samples, updated_at=now()",
        [day.isoformat(), _DAY_START.format(d=day.isoformat()),
         _DAY_START.format(d=nxt.isoformat())],
    )
    return r.get("rowcount") or 0


def _rolled_up(day: dt.date) -> bool:
    r = _db("select 1 from saj_daily_energy where day=$1::date limit 1",
            [day.isoformat()])
    return bool(r.get("rows"))


def _has_raw(day: dt.date) -> bool:
    nxt = day + dt.timedelta(days=1)
    r = _db("select 1 from saj_reading where ts >= $1::timestamptz "
            "and ts < $2::timestamptz limit 1",
            [_DAY_START.format(d=day.isoformat()), _DAY_START.format(d=nxt.isoformat())])
    return bool(r.get("rows"))


def prune_day(day: dt.date) -> int:
    """Delete one MYT day of 5-min rows. Refuses if that day has no rollup."""
    if not _rolled_up(day):
        raise RuntimeError(f"refusing to prune {day}: no saj_daily_energy rows")
    nxt = day + dt.timedelta(days=1)
    r = _db("delete from saj_reading where ts >= $1::timestamptz "
            "and ts < $2::timestamptz",
            [_DAY_START.format(d=day.isoformat()), _DAY_START.format(d=nxt.isoformat())])
    return r.get("rowcount") or 0


def enforce(rollup_recent_days: int = 10, dry_run: bool = False) -> dict:
    """Roll up, then prune everything older than the capture policy floor.

    Rolls up every day that is about to be pruned (all of them, not a sample),
    plus the last `rollup_recent_days` so the daily table stays current.
    """
    ensure_schema()
    floor = backfill.policy_floor()
    lo, hi = _bounds()
    out = {"policy_floor": floor.isoformat(), "dry_run": dry_run,
           "days_rolled": 0, "device_days": 0, "days_pruned": 0,
           "rows_pruned": 0, "oldest": lo.isoformat() if lo else None,
           "newest": hi.isoformat() if hi else None}
    if lo is None:
        return out

    # 1. roll up every day that will be pruned
    day = lo
    while day < floor:
        if _has_raw(day):
            out["device_days"] += rollup_day(day)
            out["days_rolled"] += 1
        day += dt.timedelta(days=1)

    # 2. keep the recent window's dailies fresh too
    recent = max(floor, (hi or floor) - dt.timedelta(days=rollup_recent_days - 1))
    day = recent
    while hi and day <= hi:
        out["device_days"] += rollup_day(day)
        out["days_rolled"] += 1
        day += dt.timedelta(days=1)

    if dry_run:
        return out

    # 3. prune, one day at a time, each guarded by its own rollup
    day = lo
    while day < floor:
        n = prune_day(day)
        if n:
            out["days_pruned"] += 1
            out["rows_pruned"] += n
        day += dt.timedelta(days=1)
    return out


def stats() -> dict:
    ensure_schema()
    lo, hi = _bounds()
    raw = _db("select count(*) as n from saj_reading").get("rows")[0]["n"]
    daily = _db("select count(*) as n, min(day) as lo, max(day) as hi "
                "from saj_daily_energy").get("rows")[0]
    size = _db("select pg_size_pretty(pg_total_relation_size('saj_reading')) as raw, "
               "pg_size_pretty(pg_total_relation_size('saj_daily_energy')) as daily"
               ).get("rows")[0]
    return {
        "policy_months": backfill.POLICY_MONTHS,
        "policy_floor": backfill.policy_floor().isoformat(),
        "raw_rows": int(raw), "raw_oldest": lo.isoformat() if lo else None,
        "raw_newest": hi.isoformat() if hi else None, "raw_size": size["raw"],
        "daily_rows": int(daily["n"]),
        "daily_oldest": str(daily["lo"]) if daily["lo"] else None,
        "daily_newest": str(daily["hi"]) if daily["hi"] else None,
        "daily_size": size["daily"],
    }
