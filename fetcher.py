"""Fetch the 5-min generation feed from SAJ and upsert it into prod `saj_reading`.

Fetch by device serial or by plant UID. This is the write side of the pipeline —
the client app only ever reads `saj_reading` back out.

Timezone rule (must match the existing dataset): the SAJ API returns Malaysia-local
timestamps. We tag them +08:00 on insert so Postgres stores the true UTC instant.
The reader shifts back to Asia/Kuala_Lumpur for display.
"""
from __future__ import annotations

import time
import datetime as dt

import pg
import r2

# When we last pulled each device from SAJ (in-process; resets on redeploy).
# Powers the per-visit freshness gate so rapid re-opens don't re-hit the portal.
_last_pull: dict[str, float] = {}

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


def latest(device_sn: str):
    """Newest stored reading for a device — used to confirm a trigger landed."""
    r = pg.run(
        "select ts, ac_power_w, today_kwh, total_kwh from saj_reading "
        "where device_sn=$1 order by ts desc limit 1",
        [device_sn],
    )
    rows = r.get("rows") or []
    return rows[0] if rows else None


# ---- device model / static info (from baseInverterDetail) ------------------
def device_info(device_sn: str) -> dict | None:
    """Stored model info for a device, or None if the device isn't in the catalog."""
    r = pg.run(
        "select device_sn, model, rated_power_kw, phase_name, firmware, image_url, "
        "device_type, plant_uid from saj_device where device_sn=$1",
        [device_sn],
    )
    rows = r.get("rows") or []
    return rows[0] if rows else None


def _upsert_device_info(sn: str, b: dict):
    """Persist the human-readable model fields from a baseInverterDetail payload.

    Mirrors the model image into our R2 bucket and stores OUR URL; if R2 is
    unconfigured or the mirror fails, falls back to the original SAJ-cloud URL.
    """
    saj_pic = b.get("inverterPic")
    image_url = r2.mirror_image(saj_pic) or saj_pic
    pg.run(
        "insert into saj_device (device_sn, model, rated_power_kw, phase_name, "
        "firmware, image_url, updated_at) values ($1,$2,$3,$4,$5,$6, now()) "
        "on conflict (device_sn) do update set model=excluded.model, "
        "rated_power_kw=excluded.rated_power_kw, phase_name=excluded.phase_name, "
        "firmware=excluded.firmware, image_url=excluded.image_url, updated_at=now()",
        [sn, b.get("inverterModel"), _f(b.get("ratedPower")), b.get("phaseName"),
         b.get("displayFw"), image_url],
    )


def ensure_device_info(client, device_sn: str, force: bool = False) -> dict | None:
    """Populate model info if missing (or `force`), then return it.

    One SAJ call per device ever: once `model` is stored, later calls short-circuit.
    """
    row = device_info(device_sn)
    if not force and row and row.get("model"):
        return row
    b = client.inverter_base(device_sn)
    _upsert_device_info(device_sn, b)
    return device_info(device_sn)


def fetch_device(client, device_sn: str, days: int = 1,
                 fresh_seconds: int = 0, force: bool = False) -> dict:
    """Pull `days` of 5-min data (today back) for one device SN into saj_reading.

    `days=1` = today only (morning -> now), the per-visit / nightly trigger.
    Larger values backfill history in one call.

    Freshness gate (per-visit efficiency): when `fresh_seconds` > 0 and `days==1`,
    if we already pulled this device from SAJ within the last `fresh_seconds`,
    skip the portal call and serve what's already in the DB — so rapid re-opens
    of the app don't hammer SAJ (data is only 5-min cadence anyway). `force=True`
    always pulls. The nightly sweep passes `fresh_seconds=0`, so it always does a
    full pull.

    Returns {"rows_written": int, "source": "cache"|"live", "latest": row}.
    """
    if not force and fresh_seconds and days == 1:
        last = _last_pull.get(device_sn)
        if last is not None and (time.time() - last) < fresh_seconds:
            return {"rows_written": 0, "source": "cache", "latest": latest(device_sn)}

    total = 0
    today = dt.date.today()
    for i in range(days):
        day = (today - dt.timedelta(days=i)).isoformat()
        rows = client.raw_data_day(device_sn, day)
        total += _upsert_readings(device_sn, rows)
    _last_pull[device_sn] = time.time()
    # Opportunistically fill model info while we're already talking to SAJ.
    # Best-effort: model is secondary, so never let it fail the data fetch.
    try:
        ensure_device_info(client, device_sn)
    except Exception as e:  # noqa: BLE001
        print(f"[device-info] {device_sn} skip: {e}", flush=True)
    return {"rows_written": total, "source": "live", "latest": latest(device_sn)}


def fetch_plant(client, plant_uid: str, days: int = 1,
                fresh_seconds: int = 0, force: bool = False) -> dict:
    """Resolve the plant's device SNs live from the portal, fetch each.

    Returns {device_sn: <fetch_device result>}. Works without a synced catalog.
    """
    sns = client.plant_device_sns(plant_uid)
    return {
        sn: fetch_device(client, sn, days=days, fresh_seconds=fresh_seconds, force=force)
        for sn in sns
    }


# ---- historical backfill: skip-aware, for the wide (e.g. 31-day) window ----
def _day_has_rows(device_sn: str, day: str) -> bool:
    """True if `saj_reading` already holds any row for this device on this MYT day."""
    r = pg.run(
        "select 1 from saj_reading where device_sn=$1 "
        "and (ts at time zone 'Asia/Kuala_Lumpur')::date = $2::date limit 1",
        [device_sn, day],
    )
    return bool(r.get("rows"))


def fetch_device_history(client, device_sn: str, days: int = 31,
                         force: bool = False) -> dict:
    """Backfill up to `days` of history for one device, cheaply.

    Past days are immutable once stored, so any past MYT day that already has rows
    is skipped — only genuinely missing history is pulled from SAJ. Today (i==0) is
    always re-pulled since it's still accumulating. `force=True` re-pulls every day.

    Unlike `fetch_device`, this is meant for the wide monthly window the app calls
    on demand: the first call backfills the month; later calls only touch today.

    Returns {"rows_written", "days_pulled", "days_skipped", "latest"}.
    """
    total = pulled = skipped = 0
    today = dt.date.today()
    for i in range(days):
        day = (today - dt.timedelta(days=i)).isoformat()
        if i != 0 and not force and _day_has_rows(device_sn, day):
            skipped += 1
            continue
        rows = client.raw_data_day(device_sn, day)
        total += _upsert_readings(device_sn, rows)
        pulled += 1
    _last_pull[device_sn] = time.time()
    # Opportunistically fill model info while we're already talking to SAJ.
    try:
        ensure_device_info(client, device_sn)
    except Exception as e:  # noqa: BLE001
        print(f"[device-info] {device_sn} skip: {e}", flush=True)
    return {"rows_written": total, "days_pulled": pulled,
            "days_skipped": skipped, "latest": latest(device_sn)}


def fetch_plant_history(client, plant_uid: str, days: int = 31,
                        force: bool = False) -> dict:
    """Skip-aware historical backfill for every device in a plant.

    Returns {device_sn: <fetch_device_history result>}.
    """
    sns = client.plant_device_sns(plant_uid)
    return {
        sn: fetch_device_history(client, sn, days=days, force=force)
        for sn in sns
    }


# ---- read side: chart-ready series straight from prod (no SAJ call) --------
def _myt_since(days: int) -> str:
    """First MYT calendar day to include, for a `days`-window ending today."""
    myt_today = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()
    return (myt_today - dt.timedelta(days=days - 1)).isoformat()


def plant_sns(plant_uid: str) -> list[str]:
    """Device SNs for a plant from the catalog (fast, no SAJ call)."""
    r = pg.run("select device_sn from saj_device where plant_uid=$1 order by device_sn",
               [plant_uid])
    return [row["device_sn"] for row in r.get("rows", [])]


def series_for_sns(sns: list[str], days: int = 1) -> list[dict]:
    """AC-power curve (5-min), summed across the given devices. [{ts, ac_power_w}]."""
    if not sns:
        return []
    ph = ",".join(f"${i + 2}" for i in range(len(sns)))
    sql = (
        "select r.ts, sum(coalesce(r.ac_power_w,0)) as ac_power_w from saj_reading r "
        f"where r.device_sn in ({ph}) "
        "and (r.ts at time zone 'Asia/Kuala_Lumpur')::date >= $1::date "
        "group by r.ts order by r.ts"
    )
    r = pg.run(sql, [_myt_since(days), *sns])
    return r.get("rows", [])


def daily_for_sns(sns: list[str], days: int = 1) -> list[dict]:
    """Per-day generation (kWh), summed across devices. [{day, kwh}].

    Per device per day = max(today_kwh) (it's cumulative), then summed.
    """
    if not sns:
        return []
    ph = ",".join(f"${i + 2}" for i in range(len(sns)))
    sql = (
        "select day, sum(kwh) as kwh from ("
        "  select r.device_sn, (r.ts at time zone 'Asia/Kuala_Lumpur')::date as day, "
        "         max(coalesce(r.today_kwh,0)) as kwh from saj_reading r "
        f"  where r.device_sn in ({ph}) "
        "  and (r.ts at time zone 'Asia/Kuala_Lumpur')::date >= $1::date "
        "  group by r.device_sn, 2"
        ") t group by day order by day"
    )
    r = pg.run(sql, [_myt_since(days), *sns])
    return r.get("rows", [])
