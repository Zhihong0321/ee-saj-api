"""Resumable one-time historical copy of the SAJ 5-min feed into `saj_reading`.

Driven from the /backfill page in the browser: [Start] / [Stop], resume later.

Why this exists separately from `sync_all`: SAJ only serves one device-day per
call (multi-day ranges are silently clamped, multi-device is rejected), so a
full-history copy is ~240k calls and takes hours. That cannot live in an HTTP
request, and it cannot keep its progress in memory — a Railway redeploy would
lose it. So:

  * progress is a row per device in `saj_backfill_device`, advanced after EVERY
    device-day, so a stop/crash/redeploy resumes at most one day behind;
  * the run state lives in `saj_backfill_job`, so the page (and a restarted
    process) can see what is going on;
  * each worker holds its OWN SajClient on its OWN SAJ account, because the
    portal allows a single active session per account — sharing the live
    service's account would log the customer-facing fetches out all night.

Devices are claimed one at a time with FOR UPDATE SKIP LOCKED, so workers never
collide and a worker that dies mid-device releases its claim after
CLAIM_TTL_MIN and someone else picks it up.

Retention policy: we keep at most BACKFILL_MONTHS (default 4) months of
history. The floor is recomputed from today on every Start, so it is a rolling
window, and it is enforced as a clamp — an explicit `window_start` older than
the policy is pulled forward, never honoured.

Env:
  BACKFILL_USERS          comma-separated SAJ accounts (default operation02,03,04)
  BACKFILL_PASS           password for those accounts (default SAJ_PASS)
  BACKFILL_MONTHS         how many months of history to keep (default 4)
  BACKFILL_REQ_INTERVAL   extra sleep between calls per worker (default 0.05s)
"""
from __future__ import annotations

import os
import time
import calendar
import threading
import datetime as dt

import pg
import fetcher
import sync_customer_plants
from saj_api import SajClient

# Capture policy: keep this many months of history, and no more. Rolling —
# recomputed from today at every Start, not a fixed date.
# (For reference, SAJ itself stops serving 5-min raw data before ~2025-12-01:
# an identical wall on every device tested, including 3-year-old inverters. So
# anything beyond ~8 months is unavailable regardless of this policy.)
POLICY_MONTHS = int(os.environ.get("BACKFILL_MONTHS", "4"))
USERS = [u.strip() for u in
         os.environ.get("BACKFILL_USERS", "operation02,operation03,operation04").split(",")
         if u.strip()]
PASSWORD = os.environ.get("BACKFILL_PASS") or os.environ.get("SAJ_PASS")
REQ_INTERVAL = float(os.environ.get("BACKFILL_REQ_INTERVAL", "0.05"))
CLAIM_TTL_MIN = 15

# pg keeps one module-level connection; serialise our worker threads on it.
_db_lock = threading.Lock()
_stop = threading.Event()
_threads: list[threading.Thread] = []
_threads_lock = threading.Lock()
_start_lock = threading.Lock()

# Live per-worker view for the page (best-effort, not persisted).
_worker_now: dict[str, str] = {}


def _db(sql, params=None):
    with _db_lock:
        r = pg.run(sql, params)
    if isinstance(r, dict) and "error" in r:
        raise RuntimeError(f"db: {r}")
    return r


def _rows(sql, params=None):
    return _db(sql, params).get("rows") or []


# ---- schema ---------------------------------------------------------------
def ensure_schema():
    _db("""create table if not exists saj_backfill_job (
             id           int primary key,
             state        text not null,
             window_start date not null,
             window_end   date not null,
             workers      int  not null default 1,
             started_at   timestamptz,
             stopped_at   timestamptz,
             message      text,
             sync_state   text not null default 'pending',
             sync_plants  int not null default 0,
             sync_devices int not null default 0,
             sync_plant_links int not null default 0,
             sync_device_maps int not null default 0,
             sync_unmatched int not null default 0,
             sync_ambiguous int not null default 0,
             sync_conflicts int not null default 0,
             sync_error text,
             sync_started_at timestamptz,
             sync_finished_at timestamptz,
             updated_at   timestamptz not null default now())""")
    _db("""alter table saj_backfill_job
             add column if not exists sync_state text not null default 'pending',
             add column if not exists sync_plants int not null default 0,
             add column if not exists sync_devices int not null default 0,
             add column if not exists sync_plant_links int not null default 0,
             add column if not exists sync_device_maps int not null default 0,
             add column if not exists sync_unmatched int not null default 0,
             add column if not exists sync_ambiguous int not null default 0,
             add column if not exists sync_conflicts int not null default 0,
             add column if not exists sync_error text,
             add column if not exists sync_started_at timestamptz,
             add column if not exists sync_finished_at timestamptz""")
    _db("""create table if not exists saj_backfill_device (
             device_sn    text primary key,
             cursor_day   date,
             done         boolean not null default false,
             days_done    int    not null default 0,
             rows_written bigint not null default 0,
             claimed_by   text,
             claimed_at   timestamptz,
             error        text,
             updated_at   timestamptz not null default now())""")
    _db("""create index if not exists saj_backfill_device_todo_idx
             on saj_backfill_device (done, claimed_at)""")


def _seed_devices() -> int:
    """Add any catalog device missing from the progress table. Idempotent."""
    r = _db("""insert into saj_backfill_device (device_sn)
               select d.device_sn from saj_device d
               on conflict (device_sn) do nothing""")
    return r.get("rowcount") or 0


def _sync_catalog() -> dict:
    """Refresh plants/devices and apply safe customer links before history copy."""
    summary = sync_customer_plants.run(apply=True, db_lock=_db_lock)
    applied = summary.get("applied") or {}
    return {
        "plants": int(summary.get("portal_plants") or 0),
        "devices": int(summary.get("portal_devices") or 0),
        "plant_links": int(applied.get("plants_linked") or 0),
        "device_maps": int(applied.get("device_maps_inserted") or 0),
        "unmatched": int(summary.get("unmatched_plants") or 0),
        "ambiguous": int(summary.get("ambiguous_plants") or 0),
        "conflicts": (int(summary.get("conflict_plants") or 0)
                      + int(summary.get("invalid_existing_links") or 0)),
    }


# ---- job state ------------------------------------------------------------
def job() -> dict | None:
    rows = _rows("select * from saj_backfill_job where id=1")
    return rows[0] if rows else None


def _set_state(state: str, **cols):
    sets = ["state=$1", "updated_at=now()"]
    params = [state]
    for k, v in cols.items():
        params.append(v)
        sets.append(f"{k}=${len(params)}")
    _db(f"update saj_backfill_job set {','.join(sets)} where id=1", params)


def _myt_today() -> dt.date:
    return (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()


def _window_end() -> dt.date:
    """Yesterday in MYT — today is still accumulating and belongs to the nightly job."""
    return _myt_today() - dt.timedelta(days=1)


def _months_ago(d: dt.date, months: int) -> dt.date:
    m, y = d.month - months, d.year
    while m <= 0:
        m += 12
        y -= 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def policy_floor() -> dt.date:
    """Oldest day we are allowed to capture, as of now."""
    return _months_ago(_myt_today(), POLICY_MONTHS)


# ---- SAJ history -----------------------------------------------------------
def _history_day(client, sn: str, day: str) -> list:
    """Completed-day curve via compact chart data, with raw fallback."""
    return fetcher._completed_day_rows(client, sn, day)


def _existing_days(sn: str, start: dt.date, end: dt.date) -> set:
    rows = _rows(
        "select distinct (ts at time zone 'Asia/Kuala_Lumpur')::date as d "
        "from saj_reading where device_sn=$1 "
        "and (ts at time zone 'Asia/Kuala_Lumpur')::date between $2::date and $3::date",
        [sn, start.isoformat(), end.isoformat()],
    )
    return {r["d"] if isinstance(r["d"], dt.date) else
            dt.date.fromisoformat(str(r["d"])) for r in rows}


# ---- worker ---------------------------------------------------------------
def _claim(worker: str) -> dict | None:
    rows = _rows(
        "update saj_backfill_device b set claimed_by=$1, claimed_at=now(), "
        "updated_at=now() where b.device_sn = ("
        "  select device_sn from saj_backfill_device"
        "  where not done and (claimed_by is null or claimed_at < now() - interval "
        f" '{CLAIM_TTL_MIN} minutes')"
        "  order by device_sn for update skip locked limit 1) "
        "returning b.device_sn, b.cursor_day, b.days_done, b.rows_written",
        [worker],
    )
    return rows[0] if rows else None


def _as_date(v) -> dt.date | None:
    if v is None:
        return None
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v)[:10])


def _worker(worker: str, user: str, win_start: dt.date, win_end: dt.date):
    try:
        client = SajClient(username=user, password=PASSWORD)
    except Exception as e:  # noqa: BLE001
        print(f"[backfill:{worker}] client init failed: {e}", flush=True)
        return
    print(f"[backfill:{worker}] up on {user} {win_start}..{win_end}", flush=True)

    while not _stop.is_set():
        try:
            dev = _claim(worker)
        except Exception as e:  # noqa: BLE001
            print(f"[backfill:{worker}] claim failed: {e}", flush=True)
            time.sleep(5)
            continue
        if not dev:
            break  # nothing left to claim

        sn = dev["device_sn"]
        day = _as_date(dev["cursor_day"]) or win_start
        days_done = dev["days_done"] or 0
        written = dev["rows_written"] or 0
        try:
            have = _existing_days(sn, day, win_end)
        except Exception as e:  # noqa: BLE001
            print(f"[backfill:{worker}] {sn} existing-days failed: {e}", flush=True)
            have = set()

        while day <= win_end and not _stop.is_set():
            _worker_now[worker] = f"{sn} {day}"
            try:
                if day not in have:
                    rows = _history_day(client, sn, day.isoformat())
                    if rows:
                        with _db_lock:
                            n = fetcher._upsert_readings(sn, rows)
                        written += n
                    if REQ_INTERVAL:
                        time.sleep(REQ_INTERVAL)
                days_done += 1
                nxt = day + dt.timedelta(days=1)
                _db("update saj_backfill_device set cursor_day=$1, days_done=$2, "
                    "rows_written=$3, error=null, updated_at=now() where device_sn=$4",
                    [nxt.isoformat(), days_done, written, sn])
                day = nxt
            except Exception as e:  # noqa: BLE001 — one bad day never kills the sweep
                msg = f"{day}: {e}"[:400]
                print(f"[backfill:{worker}] {sn} {msg}", flush=True)
                try:
                    _db("update saj_backfill_device set error=$1, updated_at=now() "
                        "where device_sn=$2", [msg, sn])
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(2)
                day += dt.timedelta(days=1)  # skip the bad day, keep going

        finished = day > win_end
        try:
            _db("update saj_backfill_device set done=$1, cursor_day=$2, days_done=$3, "
                "rows_written=$4, claimed_by=null, claimed_at=null, updated_at=now() "
                "where device_sn=$5",
                [finished, day.isoformat(), days_done, written, sn])
        except Exception as e:  # noqa: BLE001
            print(f"[backfill:{worker}] {sn} release failed: {e}", flush=True)

    _worker_now.pop(worker, None)
    print(f"[backfill:{worker}] exiting", flush=True)
    _maybe_finish()


def _maybe_finish():
    """Last worker out sets the terminal job state."""
    with _threads_lock:
        if any(t.is_alive() and t is not threading.current_thread() for t in _threads):
            return
    try:
        left = _rows("select count(*) as n from saj_backfill_device where not done")
        remaining = int(left[0]["n"]) if left else 0
        if _stop.is_set():
            _set_state("stopped", stopped_at=dt.datetime.now(dt.timezone.utc),
                       message=f"stopped with {remaining} devices remaining")
        elif remaining == 0:
            _set_state("done", stopped_at=dt.datetime.now(dt.timezone.utc),
                       message="history copy and plant/customer sync complete")
        else:
            _set_state("stopped", stopped_at=dt.datetime.now(dt.timezone.utc),
                       message=f"workers exited with {remaining} devices remaining")
    except Exception as e:  # noqa: BLE001
        print(f"[backfill] finish bookkeeping failed: {e}", flush=True)


# ---- control --------------------------------------------------------------
def start(window_start: str | None = None) -> dict:
    """Synchronize the catalog, then begin (or resume) the history copy."""
    if not _start_lock.acquire(blocking=False):
        return {"status": "already_running", **status()}
    try:
        return _start(window_start)
    finally:
        _start_lock.release()


def _start(window_start: str | None = None) -> dict:
    if not PASSWORD:
        raise RuntimeError("BACKFILL_PASS / SAJ_PASS not set")
    ensure_schema()
    j = job()
    if j and j["state"] in ("syncing", "running", "stopping") and _alive():
        return {"status": "already_running", **status()}

    prev_ws = _as_date(j["window_start"]) if j else None
    floor = policy_floor()
    requested = (dt.date.fromisoformat(window_start) if window_start
                 else (prev_ws or floor))
    # The policy is a clamp, not a default: an older request is pulled forward.
    ws = max(requested, floor)
    if requested < floor:
        print(f"[backfill] {requested} is older than the {POLICY_MONTHS}-month "
              f"policy; clamped to {floor}", flush=True)
    we = _window_end()
    _stop.clear()

    if j:
        _db("update saj_backfill_job set state='syncing', window_start=$1, window_end=$2, "
            "workers=$3, started_at=coalesce(started_at, now()), stopped_at=null, "
            "message='syncing SAJ plants, devices, and customers', "
            "sync_state='running', sync_plants=0, sync_devices=0, sync_plant_links=0, "
            "sync_device_maps=0, sync_unmatched=0, sync_ambiguous=0, sync_conflicts=0, "
            "sync_error=null, sync_started_at=now(), sync_finished_at=null, "
            "updated_at=now() where id=1",
            [ws.isoformat(), we.isoformat(), len(USERS)])
    else:
        _db("insert into saj_backfill_job "
            "(id,state,window_start,window_end,workers,started_at,message,"
            "sync_state,sync_started_at) "
            "values (1,'syncing',$1,$2,$3,now(),"
            "'syncing SAJ plants, devices, and customers','running',now())",
            [ws.isoformat(), we.isoformat(), len(USERS)])

    try:
        synced = _sync_catalog()
    except Exception as e:  # noqa: BLE001
        msg = f"catalog/customer sync failed: {e}"[:400]
        _set_state("failed", stopped_at=dt.datetime.now(dt.timezone.utc), message=msg,
                   sync_state="failed", sync_error=msg,
                   sync_finished_at=dt.datetime.now(dt.timezone.utc))
        raise RuntimeError(msg) from e

    review = synced["unmatched"] + synced["ambiguous"] + synced["conflicts"]
    sync_message = (
        f"catalog synced: {synced['plants']} plants, {synced['devices']} devices; "
        f"linked {synced['plant_links']} plants and {synced['device_maps']} devices; "
        f"{review} plants need review"
    )
    _set_state("syncing", message=sync_message, sync_state="done",
               sync_plants=synced["plants"], sync_devices=synced["devices"],
               sync_plant_links=synced["plant_links"],
               sync_device_maps=synced["device_maps"],
               sync_unmatched=synced["unmatched"],
               sync_ambiguous=synced["ambiguous"],
               sync_conflicts=synced["conflicts"],
               sync_error=None, sync_finished_at=dt.datetime.now(dt.timezone.utc))
    if _stop.is_set():
        _set_state("stopped", stopped_at=dt.datetime.now(dt.timezone.utc),
                   message=f"stopped after {sync_message}")
        return {"status": "stopped", **status()}

    try:
        seeded = _seed_devices()

        # Widening the window backwards must re-open devices already marked done,
        # or the older days would never be visited. Days already in saj_reading are
        # still skipped per-device, so re-opening costs no extra SAJ calls.
        if prev_ws and ws < prev_ws:
            _db("update saj_backfill_device set done=false, cursor_day=null, days_done=0, "
                "claimed_by=null, claimed_at=null, updated_at=now()")
            print(f"[backfill] window widened {prev_ws} -> {ws}; re-opened all devices",
                  flush=True)

        # Days pass between runs, so a device that finished against an older
        # window_end is no longer actually complete. Re-open those; the per-device
        # skip of days already stored keeps this cheap.
        _db("update saj_backfill_device set done=false, claimed_by=null, claimed_at=null, "
            "updated_at=now() where done and (cursor_day is null or cursor_day <= $1::date)",
            [we.isoformat()])
        _db("update saj_backfill_job set state='running', window_start=$1, window_end=$2, "
            "workers=$3, stopped_at=null, message=$4, updated_at=now() where id=1",
            [ws.isoformat(), we.isoformat(), len(USERS),
             f"{sync_message}; {seeded} new devices queued"])
    except Exception as e:  # noqa: BLE001
        msg = f"backfill preparation failed after catalog sync: {e}"[:400]
        _set_state("failed", stopped_at=dt.datetime.now(dt.timezone.utc), message=msg)
        raise RuntimeError(msg) from e

    with _threads_lock:
        _threads.clear()
        for i, user in enumerate(USERS):
            name = f"w{i + 1}"
            t = threading.Thread(target=_worker, args=(name, user, ws, we),
                                 name=f"backfill-{name}", daemon=True)
            _threads.append(t)
    for t in _threads:
        t.start()
    return {"status": "started", **status()}


def stop() -> dict:
    _stop.set()
    try:
        _set_state("stopping", message="stop requested")
    except Exception:  # noqa: BLE001
        pass
    return {"status": "stopping", **status()}


def _alive() -> bool:
    with _threads_lock:
        return any(t.is_alive() for t in _threads)


def resume_if_interrupted():
    """Called on app startup: a job left 'running' means we were killed mid-copy."""
    try:
        ensure_schema()
        j = job()
    except Exception as e:  # noqa: BLE001
        print(f"[backfill] startup check skipped: {e}", flush=True)
        return
    if j and j["state"] in ("syncing", "running", "stopping") and not _alive():
        print("[backfill] job was interrupted — auto-resuming", flush=True)
        try:
            start()
        except Exception as e:  # noqa: BLE001
            print(f"[backfill] auto-resume failed: {e}", flush=True)


# ---- reporting ------------------------------------------------------------
def status() -> dict:
    try:
        ensure_schema()
        j = job()
    except Exception as e:  # noqa: BLE001
        return {"state": "unavailable", "error": str(e)}
    agg = _rows("""select count(*) as devices,
                          count(*) filter (where done) as devices_done,
                          count(*) filter (where error is not null) as devices_errored,
                          coalesce(sum(days_done),0) as days_done,
                          coalesce(sum(rows_written),0) as rows_written
                   from saj_backfill_device""")
    a = agg[0] if agg else {}
    ws = _as_date(j["window_start"]) if j else None
    we = _as_date(j["window_end"]) if j else None
    span = ((we - ws).days + 1) if (ws and we) else 0
    devices = int(a.get("devices") or 0)
    days_done = int(a.get("days_done") or 0)
    total_days = devices * span
    started = j.get("started_at") if j else None
    elapsed = None
    if started:
        if isinstance(started, str):
            started = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
        elapsed = (dt.datetime.now(dt.timezone.utc)
                   - started.replace(tzinfo=started.tzinfo or dt.timezone.utc)).total_seconds()
    return {
        "state": (j or {}).get("state", "idle"),
        "message": (j or {}).get("message"),
        "window_start": ws.isoformat() if ws else None,
        "window_end": we.isoformat() if we else None,
        "span_days": span,
        "devices": devices,
        "devices_done": int(a.get("devices_done") or 0),
        "devices_errored": int(a.get("devices_errored") or 0),
        "device_days_done": days_done,
        "device_days_total": total_days,
        "pct": round(100.0 * days_done / total_days, 2) if total_days else 0.0,
        "rows_written": int(a.get("rows_written") or 0),
        "sync_state": (j or {}).get("sync_state", "pending"),
        "sync_plants": int((j or {}).get("sync_plants") or 0),
        "sync_devices": int((j or {}).get("sync_devices") or 0),
        "sync_plant_links": int((j or {}).get("sync_plant_links") or 0),
        "sync_device_maps": int((j or {}).get("sync_device_maps") or 0),
        "sync_unmatched": int((j or {}).get("sync_unmatched") or 0),
        "sync_ambiguous": int((j or {}).get("sync_ambiguous") or 0),
        "sync_conflicts": int((j or {}).get("sync_conflicts") or 0),
        "sync_error": (j or {}).get("sync_error"),
        "policy_months": POLICY_MONTHS,
        "policy_floor": policy_floor().isoformat(),
        "workers_configured": len(USERS),
        "workers_alive": sum(1 for t in _threads if t.is_alive()),
        "worker_now": dict(_worker_now),
        "elapsed_seconds": int(elapsed) if elapsed else None,
        "accounts": USERS,
    }


def recent_errors(limit: int = 10) -> list:
    return _rows("select device_sn, error, updated_at from saj_backfill_device "
                 "where error is not null order by updated_at desc limit $1", [limit])
