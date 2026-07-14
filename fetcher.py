"""Fetch the 5-min generation feed from SAJ and upsert it into prod `saj_reading`.

Fetch by device serial or by plant UID. This is the write side of the pipeline —
the client app only ever reads `saj_reading` back out.

Timezone rule (must match the existing dataset): the SAJ API returns Malaysia-local
timestamps. We tag them +08:00 on insert so Postgres stores the true UTC instant.
The reader shifts back to Asia/Kuala_Lumpur for display.
"""
from __future__ import annotations

import datetime as dt

import pg

# saj_reading columns we populate (raw jsonb left null — charts don't need it)
_COLS = ["device_sn", "ts", "ac_power_w", "pv_power_w", "today_kwh",
         "month_kwh", "year_kwh", "total_kwh", "device_temp"]


def _f(x):
    try:
        if x in (None, "", "--"):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _upsert_readings(sn: str, rows: list[dict], batch: int = 200) -> int:
    """Upsert raw 5-min rows for one device. Returns rows written."""
    n = len(_COLS)
    tuples = []
    for r in rows:
        ts = r.get("datetime")
        if not ts:
            continue
        ts = f"{ts}+08:00"  # tag Malaysia-local so PG stores the true UTC instant
        tuples.append((sn, ts, _f(r.get("pac")), _f(r.get("PVP")),
                       _f(r.get("todayPVEnergy")), _f(r.get("monthPVEnergy")),
                       _f(r.get("yearPVEnergy")), _f(r.get("totalPVEnergy")),
                       _f(r.get("deviceTemp"))))
    written = 0
    for i in range(0, len(tuples), batch):
        chunk = tuples[i:i + batch]
        ph = ",".join("(" + ",".join(f"${j * n + k + 1}" for k in range(n)) + ")"
                      for j in range(len(chunk)))
        params = [v for t in chunk for v in t]
        sql = (f"insert into saj_reading ({','.join(_COLS)}) values {ph} "
               "on conflict (device_sn,ts) do update set "
               "ac_power_w=excluded.ac_power_w, pv_power_w=excluded.pv_power_w, "
               "today_kwh=excluded.today_kwh, month_kwh=excluded.month_kwh, "
               "year_kwh=excluded.year_kwh, total_kwh=excluded.total_kwh, "
               "device_temp=excluded.device_temp")
        res = pg.run(sql, params)
        if isinstance(res, dict) and "error" in res:
            raise RuntimeError(f"upsert {sn} failed: {res}")
        written += len(chunk)
    return written


def fetch_device(client, device_sn: str, days: int = 1) -> int:
    """Pull `days` of 5-min data (today back) for one device SN into saj_reading.

    Returns total rows written. `days=1` = today only (the freshness trigger);
    larger values backfill history in one call.
    """
    total = 0
    today = dt.date.today()
    for i in range(days):
        day = (today - dt.timedelta(days=i)).isoformat()
        rows = client.raw_data_day(device_sn, day)
        total += _upsert_readings(device_sn, rows)
    return total


def fetch_plant(client, plant_uid: str, days: int = 1) -> dict:
    """Resolve the plant's device SNs live from the portal, fetch each.

    Returns {device_sn: rows_written}. Works without a synced catalog.
    """
    sns = client.plant_device_sns(plant_uid)
    result = {}
    for sn in sns:
        result[sn] = fetch_device(client, sn, days=days)
    return result


def latest(device_sn: str):
    """Newest stored reading for a device — used to confirm a trigger landed."""
    r = pg.run(
        "select ts, ac_power_w, today_kwh, total_kwh from saj_reading "
        "where device_sn=$1 order by ts desc limit 1",
        [device_sn],
    )
    rows = r.get("rows") or []
    return rows[0] if rows else None
