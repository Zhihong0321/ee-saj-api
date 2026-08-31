"""EE SAJ Data Fetcher — HTTP API on Railway.

Trigger a fetch of the 5-min generation feed from the SAJ portal into prod
`saj_reading`, keyed by device serial or plant UID.

    POST /fetch/device/{device_sn}?days=1[&force=true]   one inverter
    POST /fetch/plant/{plant_uid}?days=1[&force=true]     every inverter in a plant
    POST /sync/fast?customer=NAME | ?plant=NAME           one named account, by name
    GET  /fast                                            fast-sync control page (browser)
    GET  /sync/fast/log                                   recent fast syncs + debug log
    GET  /device/{device_sn}/latest                       confirm what landed
    GET  /backfill                                        one-time history copy UI
    GET  /health                                          liveness + config

Two intended use modes:
  * Daytime, per-visit: the app POSTs /fetch/plant/{uid} when a customer opens
    their dashboard -> today's data (morning -> now) is pulled into the DB.
    A freshness gate (VISIT_FRESH_SECONDS) skips the SAJ call when the stored
    data is already fresh, so rapid re-opens don't hammer the portal.
  * Nightly 23:00 MYT: a Railway Cron runs sync_all.py -> full sweep of every
    device for the complete day (always a full pull, no gate).

Separate from both: /backfill is the one-time copy of all retained history
(see backfill.py) — a worker pool with its progress in Postgres, started and
stopped from a page in the browser and resumable across redeploys.

Auth: if TRIGGER_TOKEN is set, every /fetch call must present it as `?token=...`
or header `X-Trigger-Token: ...`.

The SAJ account is single-session, so all portal calls are serialised behind one
shared client + lock.
"""
from __future__ import annotations

import os
import time
import random
import threading
import datetime as dt

from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import HTMLResponse

import fetcher
import pg
import r2
import backfill
import fast_sync
import retention
from backfill_page import PAGE as BACKFILL_PAGE
from fast_sync_page import PAGE as FAST_SYNC_PAGE
from saj_api import SajClient, SajError

SAJ_USER = os.environ.get("SAJ_USER")
SAJ_PASS = os.environ.get("SAJ_PASS")
TRIGGER_TOKEN = os.environ.get("TRIGGER_TOKEN")
MAX_DAYS = int(os.environ.get("MAX_DAYS", "14"))
# Wide window for the on-demand monthly backfill + reads (app-chosen, heavier).
HISTORY_MAX_DAYS = int(os.environ.get("HISTORY_MAX_DAYS", "31"))
# per-visit gate: skip the SAJ pull if today's newest stored reading is younger
# than this (data is 5-min cadence, so ~4 min avoids redundant pulls on refresh).
VISIT_FRESH_SECONDS = int(os.environ.get("VISIT_FRESH_SECONDS", "240"))

app = FastAPI(title="EE SAJ Data Fetcher", version="1.3.0")

_lock = threading.Lock()
_client: SajClient | None = None


def _get_client() -> SajClient:
    global _client
    if _client is None:
        if not (SAJ_USER and SAJ_PASS):
            raise HTTPException(500, "SAJ_USER / SAJ_PASS env vars are not set")
        _client = SajClient(username=SAJ_USER, password=SAJ_PASS)
    return _client


def _check_auth(token: str | None):
    if TRIGGER_TOKEN and token != TRIGGER_TOKEN:
        raise HTTPException(401, "missing or invalid trigger token")


@app.get("/")
@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "ee-saj-api",
        "version": app.version,
        "revision": os.environ.get("RAILWAY_GIT_COMMIT_SHA"),
        "deployment_id": os.environ.get("RAILWAY_DEPLOYMENT_ID"),
        "environment": os.environ.get("RAILWAY_ENVIRONMENT_NAME"),
        "db_backend": pg.backend(),
        "saj_account": SAJ_USER,
        "protected": bool(TRIGGER_TOKEN),
        "visit_fresh_seconds": VISIT_FRESH_SECONDS,
        "r2_image_mirror": r2.enabled(),
        "pages": {"fast_sync": "/fast", "backfill": "/backfill",
                  "agent": "/agent", "api_docs": "/docs"},
        "time": dt.datetime.utcnow().isoformat() + "Z",
    }


@app.post("/fetch/device/{device_sn}")
def fetch_device(
    device_sn: str,
    days: int = Query(1, ge=1, le=MAX_DAYS, description="days of history back from today"),
    force: bool = Query(False, description="bypass the freshness gate and always pull"),
    series: bool = Query(True, description="include chart-ready series + daily kWh"),
    token: str | None = Query(None),
    x_trigger_token: str | None = Header(None),
):
    """Refresh one inverter into prod, then return display-ready data (one call)."""
    _check_auth(token or x_trigger_token)
    with _lock:
        client = _get_client()
        try:
            res = fetcher.fetch_device(client, device_sn, days=days,
                                       fresh_seconds=VISIT_FRESH_SECONDS, force=force)
        except SajError as e:
            raise HTTPException(502, f"SAJ error {e.err_code}: {e.err_msg}")
    out = {"device_sn": device_sn, "days": days, "rows_written": res["rows_written"],
           "source": res["source"], "latest": res["latest"]}
    if series:
        out["series"] = fetcher.series_for_sns([device_sn], days)
        out["daily"] = fetcher.daily_for_sns([device_sn], days)
    return out


@app.post("/fetch/plant/{plant_uid}")
def fetch_plant(
    plant_uid: str,
    days: int = Query(1, ge=1, le=MAX_DAYS, description="days of history back from today"),
    force: bool = Query(False, description="bypass the freshness gate and always pull"),
    series: bool = Query(True, description="include chart-ready series + daily kWh"),
    token: str | None = Query(None),
    x_trigger_token: str | None = Header(None),
):
    """Refresh every inverter in a plant, then return the plant's display-ready data."""
    _check_auth(token or x_trigger_token)
    with _lock:
        client = _get_client()
        try:
            per_device = fetcher.fetch_plant(client, plant_uid, days=days,
                                             fresh_seconds=VISIT_FRESH_SECONDS, force=force)
        except SajError as e:
            raise HTTPException(502, f"SAJ error {e.err_code}: {e.err_msg}")
    if not per_device:
        raise HTTPException(404, f"no devices found for plant {plant_uid}")
    sns = list(per_device.keys())
    out = {
        "plant_uid": plant_uid,
        "days": days,
        "device_count": len(sns),
        "rows_written": sum(d["rows_written"] for d in per_device.values()),
        "source": "live" if any(d["source"] == "live" for d in per_device.values()) else "cache",
        "devices": sns,
    }
    if series:
        out["series"] = fetcher.series_for_sns(sns, days)
        out["daily"] = fetcher.daily_for_sns(sns, days)
    return out


# ---- wide window: on-demand 31-day (monthly) backfill ---------------------
# Separate from the fast 7-day path on purpose. Skip-aware: the first call
# backfills the whole window from SAJ; repeat calls only re-pull today, so it's
# cheap to call again. The app decides when a 30/31-day view is actually needed.
@app.post("/fetch/device/{device_sn}/last31")
def fetch_device_last31(
    device_sn: str,
    days: int = Query(31, ge=8, le=HISTORY_MAX_DAYS,
                      description="days of history back from today (wide window)"),
    force: bool = Query(False, description="re-pull every day, even already-stored past days"),
    series: bool = Query(True, description="include chart-ready series + daily kWh"),
    token: str | None = Query(None),
    x_trigger_token: str | None = Header(None),
):
    """Backfill up to `days` (default 31) for one inverter, then return the window."""
    _check_auth(token or x_trigger_token)
    with _lock:
        client = _get_client()
        try:
            res = fetcher.fetch_device_history(client, device_sn, days=days, force=force)
        except SajError as e:
            raise HTTPException(502, f"SAJ error {e.err_code}: {e.err_msg}")
    out = {"device_sn": device_sn, "days": days,
           "rows_written": res["rows_written"], "days_pulled": res["days_pulled"],
           "days_skipped": res["days_skipped"], "latest": res["latest"]}
    if series:
        out["series"] = fetcher.series_for_sns([device_sn], days)
        out["daily"] = fetcher.daily_for_sns([device_sn], days)
    return out


@app.post("/fetch/plant/{plant_uid}/last31")
def fetch_plant_last31(
    plant_uid: str,
    days: int = Query(31, ge=8, le=HISTORY_MAX_DAYS,
                      description="days of history back from today (wide window)"),
    force: bool = Query(False, description="re-pull every day, even already-stored past days"),
    series: bool = Query(True, description="include chart-ready series + daily kWh"),
    token: str | None = Query(None),
    x_trigger_token: str | None = Header(None),
):
    """Backfill up to `days` (default 31) for every inverter in a plant, then return it.

    NOTE: a cold first call pulls one SAJ request per missing day per device and runs
    synchronously — expect several seconds to ~a minute for a multi-inverter plant.
    Subsequent calls are fast (only today is re-pulled).
    """
    _check_auth(token or x_trigger_token)
    with _lock:
        client = _get_client()
        try:
            per_device = fetcher.fetch_plant_history(client, plant_uid, days=days, force=force)
        except SajError as e:
            raise HTTPException(502, f"SAJ error {e.err_code}: {e.err_msg}")
    if not per_device:
        raise HTTPException(404, f"no devices found for plant {plant_uid}")
    sns = list(per_device.keys())
    out = {
        "plant_uid": plant_uid,
        "days": days,
        "device_count": len(sns),
        "rows_written": sum(d["rows_written"] for d in per_device.values()),
        "days_pulled": sum(d["days_pulled"] for d in per_device.values()),
        "days_skipped": sum(d["days_skipped"] for d in per_device.values()),
        "devices": sns,
    }
    if series:
        out["series"] = fetcher.series_for_sns(sns, days)
        out["daily"] = fetcher.daily_for_sns(sns, days)
    return out


# ---- read-only: chart data straight from prod (no SAJ call) ---------------
@app.get("/device/{device_sn}/series")
def device_series(device_sn: str,
                  days: int = Query(1, ge=1, le=HISTORY_MAX_DAYS)):
    return {"device_sn": device_sn, "days": days,
            "series": fetcher.series_for_sns([device_sn], days),
            "daily": fetcher.daily_for_sns([device_sn], days)}


@app.get("/plant/{plant_uid}/series")
def plant_series(plant_uid: str,
                 days: int = Query(1, ge=1, le=HISTORY_MAX_DAYS)):
    sns = fetcher.plant_sns(plant_uid)
    return {"plant_uid": plant_uid, "days": days, "device_count": len(sns),
            "series": fetcher.series_for_sns(sns, days),
            "daily": fetcher.daily_for_sns(sns, days)}


@app.get("/device/{device_sn}/info")
def device_info(device_sn: str):
    """Inverter model / rated power / phase / firmware / image.

    Served from the DB; if not populated yet, fetched from SAJ once and cached.
    """
    row = fetcher.device_info(device_sn)
    if row and row.get("model"):
        return row
    with _lock:
        client = _get_client()
        try:
            info = fetcher.ensure_device_info(client, device_sn, force=True)
        except SajError as e:
            raise HTTPException(502, f"SAJ error {e.err_code}: {e.err_msg}")
    if info is None:
        raise HTTPException(404, f"device {device_sn} not found")
    return info


@app.get("/device/{device_sn}/latest")
def device_latest(device_sn: str):
    return {"device_sn": device_sn, "latest": fetcher.latest(device_sn)}


# ---- nightly full-fleet sync, triggered over HTTP by an external cron -----
# No Railway Cron service needed: this web service is already always-on, so a
# free external pinger (GitHub Actions schedule, cron-job.org, etc.) hitting
# this endpoint once a night is enough. Runs in a background thread and
# returns immediately — a full sweep takes minutes, longer than most free
# cron pingers wait for a response.
_sync_lock = threading.Lock()
_sync_state = {
    "running": False, "started_at": None, "finished_at": None,
    "total": 0, "done": 0, "ok": 0, "err": 0, "rows": 0, "last_error": None,
}


def _sync_all_worker(days: int, limit: int | None):
    interval = float(os.environ.get("SAJ_REQ_INTERVAL", "1.0"))
    jitter = float(os.environ.get("SAJ_REQ_JITTER", "0.3"))
    try:
        client = _get_client()
        r = pg.run("select device_sn from saj_device order by device_sn")
        sns = [row["device_sn"] for row in r.get("rows", [])]
        if not sns:
            sns = [sn for _, _, sn in client.iter_all_devices()]
        if limit:
            sns = sns[:limit]
        _sync_state["total"] = len(sns)
        print(f"[sync-all] devices={len(sns)} days={days} interval={interval}s", flush=True)
        for sn in sns:
            try:
                with _lock:
                    res = fetcher.fetch_device(client, sn, days=days)  # no gate -> full pull
                _sync_state["rows"] += res["rows_written"]
                _sync_state["ok"] += 1
            except Exception as e:  # noqa: BLE001 — keep the sweep alive
                _sync_state["err"] += 1
                _sync_state["last_error"] = f"{sn}: {e}"
                print(f"[sync-all] {sn} FAIL {e}", flush=True)
            _sync_state["done"] += 1
            time.sleep(interval + random.uniform(0, jitter))
        print(f"[sync-all] DONE ok={_sync_state['ok']} err={_sync_state['err']} "
              f"rows={_sync_state['rows']}", flush=True)
        # Nightly housekeeping: roll the day up and drop 5-min rows that have
        # aged out of the capture policy, so the table stops growing forever.
        try:
            print(f"[sync-all] retention: {retention.enforce()}", flush=True)
        except Exception as e:  # noqa: BLE001 — never fail the sweep over cleanup
            print(f"[sync-all] retention FAILED {e}", flush=True)
    except Exception as e:  # noqa: BLE001 — never let the thread die silently
        _sync_state["last_error"] = f"fatal: {e}"
        print(f"[sync-all] FATAL {e}", flush=True)
    finally:
        _sync_state["running"] = False
        _sync_state["finished_at"] = dt.datetime.utcnow().isoformat() + "Z"


@app.post("/sync/all")
def sync_all(
    days: int = Query(1, ge=1, le=MAX_DAYS, description="days back to pull per device"),
    limit: int | None = Query(None, description="cap device count (testing)"),
    token: str | None = Query(None),
    x_trigger_token: str | None = Header(None),
):
    """Kick off a full-fleet sweep in the background; returns immediately.

    Meant to be called once nightly by an external cron (no Railway Cron
    service needed). Refuses to start a second sweep while one is running.
    """
    _check_auth(token or x_trigger_token)
    with _sync_lock:
        if _sync_state["running"]:
            return {"status": "already_running", **_sync_state}
        _sync_state.update(running=True, started_at=dt.datetime.utcnow().isoformat() + "Z",
                           finished_at=None, total=0, done=0, ok=0, err=0, rows=0,
                           last_error=None)
        threading.Thread(target=_sync_all_worker, args=(days, limit), daemon=True).start()
    return {"status": "started", **_sync_state}


@app.get("/sync/status")
def sync_status():
    return _sync_state


# ---- fast sync: one named customer or plant -------------------------------
# Keeps the last few runs in memory so a live prod test can be inspected after
# the fact (and after an app-triggered run nobody was watching).
FAST_RUN_HISTORY = int(os.environ.get("FAST_RUN_HISTORY", "20"))
_fast_lock = threading.Lock()
_fast_runs: list[dict] = []


def _remember_fast_run(summary: dict) -> None:
    entry = {k: v for k, v in summary.items() if k != "plants"}
    entry["at"] = dt.datetime.utcnow().isoformat() + "Z"
    entry["plants"] = [{"plant_uid": p["plant_uid"], "plant_name": p["plant_name"],
                        "customer_id": p["customer_id"],
                        "devices": len(p["devices"]),
                        "rows_written": p["rows_written"]}
                       for p in summary.get("plants", [])]
    with _fast_lock:
        _fast_runs.append(entry)
        del _fast_runs[:-FAST_RUN_HISTORY]


@app.post("/sync/fast")
def sync_fast(
    customer: str | None = Query(None, description="customer name to sync"),
    plant: str | None = Query(None, description="plant name to sync"),
    customer_id: str | None = Query(None, description="exact customer id — the only "
                                    "way to pick between customers sharing a name"),
    plant_uid: str | None = Query(None, description="exact plant uid — picks one "
                                  "plant out of an ambiguous name"),
    days: int = Query(1, ge=1, le=MAX_DAYS, description="days back to pull per device"),
    refresh_catalog: bool = Query(False, description="re-read the plant row from SAJ first"),
    debug: bool = Query(True, description="include the per-step debug log in the response"),
    token: str | None = Query(None),
    x_trigger_token: str | None = Header(None),
):
    """Sync one named customer or plant — seconds, instead of the nightly sweep.

    Runs inline (a single account is a handful of devices, not the fleet), so
    the response carries the result rather than a job id. Holds the shared SAJ
    lock for the duration like the other fetch routes.

    Every response — including the 404/409/502 ones — carries the run's `log`,
    so a prod test is diagnosable from the response alone. The same lines also
    go to the service log, and the last few runs stay at `/sync/fast/log`.
    """
    _check_auth(token or x_trigger_token)
    if sum(map(bool, (customer, plant, customer_id, plant_uid))) != 1:
        raise HTTPException(
            400, "give exactly one of ?customer=, ?customer_id=, ?plant= "
                 "or ?plant_uid=")
    log = fast_sync.RunLog(debug=debug)
    try:
        with _lock:
            client = _get_client()
            out = fast_sync.run(client, customer=customer, plant=plant,
                                customer_id=customer_id, plant_uid=plant_uid,
                                days=days, refresh_catalog=refresh_catalog,
                                log=log)
        _remember_fast_run(out)
        return out
    except fast_sync.TargetAmbiguous as e:
        log.warn(f"ambiguous: {e}")
        raise HTTPException(409, {"error": "ambiguous", "query": e.query,
                                  "candidates": e.candidates[:20],
                                  "choices": e.choices[:20],
                                  "log": log.lines})
    except fast_sync.TargetNotFound as e:
        log.warn(f"not found: {e}")
        raise HTTPException(404, {"error": "not_found", "detail": str(e),
                                  "log": log.lines})
    except SajError as e:
        log.warn(f"SAJ error {e.err_code}: {e.err_msg}")
        raise HTTPException(502, {"error": "saj", "err_code": e.err_code,
                                  "detail": e.err_msg, "log": log.lines})
    except HTTPException:
        raise  # already shaped (e.g. the 500 for unset SAJ_USER) — don't re-wrap
    except Exception as e:  # noqa: BLE001 — a prod test deserves the log, not a 500
        log.warn(f"unhandled {type(e).__name__}: {e}")
        raise HTTPException(500, {"error": type(e).__name__, "detail": str(e),
                                  "log": log.lines})


@app.get("/fast", response_class=HTMLResponse)
@app.get("/sync/fast/page", response_class=HTMLResponse)
def fast_sync_page():
    """Browser control page for the fast sync — the endpoint is curl-only otherwise."""
    return HTMLResponse(FAST_SYNC_PAGE)


@app.get("/sync/fast/log")
def sync_fast_log(limit: int = Query(5, ge=1, le=FAST_RUN_HISTORY)):
    """The last few fast syncs this instance ran, newest first.

    Railway redeploys wipe this — it is a debugging convenience for a live test
    session, not an audit trail.
    """
    with _fast_lock:
        return {"runs": list(reversed(_fast_runs))[:limit], "kept": len(_fast_runs)}


# ---- one-time historical copy: browser-driven, resumable ------------------
# The full history is ~240k SAJ calls (one device-day per call — the portal
# clamps multi-day ranges and rejects multi-device), so it runs as a background
# worker pool with its progress in Postgres, driven from /backfill.
@app.get("/backfill", response_class=HTMLResponse)
def backfill_page():
    return HTMLResponse(BACKFILL_PAGE)


@app.post("/backfill/start")
def backfill_start(
    window_start: str | None = Query(None, description="oldest day to copy (YYYY-MM-DD)"),
    token: str | None = Query(None),
    x_trigger_token: str | None = Header(None),
):
    _check_auth(token or x_trigger_token)
    try:
        return backfill.start(window_start)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e))


@app.post("/backfill/stop")
def backfill_stop(
    token: str | None = Query(None),
    x_trigger_token: str | None = Header(None),
):
    _check_auth(token or x_trigger_token)
    return backfill.stop()


@app.get("/backfill/status")
def backfill_status():
    return backfill.status()


@app.get("/backfill/errors")
def backfill_errors(limit: int = Query(10, ge=1, le=100)):
    try:
        return backfill.recent_errors(limit)
    except Exception:  # noqa: BLE001
        return []


# ---- retention: daily rollup + prune of the 5-min feed --------------------
_ret_lock = threading.Lock()
_ret_state = {"running": False, "started_at": None, "finished_at": None,
              "result": None, "last_error": None}


def _retention_worker(rollup_recent_days: int, dry_run: bool):
    try:
        _ret_state["result"] = retention.enforce(rollup_recent_days, dry_run)
        print(f"[retention] {_ret_state['result']}", flush=True)
    except Exception as e:  # noqa: BLE001
        _ret_state["last_error"] = str(e)
        print(f"[retention] FAILED {e}", flush=True)
    finally:
        _ret_state["running"] = False
        _ret_state["finished_at"] = dt.datetime.utcnow().isoformat() + "Z"


@app.post("/maintenance/retention")
def maintenance_retention(
    dry_run: bool = Query(False, description="report what would happen, delete nothing"),
    rollup_recent_days: int = Query(10, ge=1, le=120),
    token: str | None = Query(None),
    x_trigger_token: str | None = Header(None),
):
    """Roll every device-day up into saj_daily_energy, then delete 5-min rows
    older than the capture policy. Returns immediately; poll the GET below."""
    _check_auth(token or x_trigger_token)
    with _ret_lock:
        if _ret_state["running"]:
            return {"status": "already_running", **_ret_state}
        _ret_state.update(running=True, started_at=dt.datetime.utcnow().isoformat() + "Z",
                          finished_at=None, result=None, last_error=None)
        threading.Thread(target=_retention_worker,
                         args=(rollup_recent_days, dry_run), daemon=True).start()
    return {"status": "started", **_ret_state}


@app.get("/maintenance/retention")
def maintenance_retention_status():
    try:
        return {**_ret_state, "stats": retention.stats()}
    except Exception as e:  # noqa: BLE001
        return {**_ret_state, "stats_error": str(e)}


# ---- ops agent (PROTOTYPE) ------------------------------------------------
# Natural-language questions about the fleet, answered by a Claude Agent SDK
# agent whose only tool is a read-only SELECT against the saj_* tables.
# Imported lazily so a missing claude-agent-sdk can never take down the fetcher.
@app.get("/agent", response_class=HTMLResponse)
def agent_page():
    from agent.agent_page import PAGE
    return PAGE


@app.get("/agent/data-requests")
def agent_data_requests():
    """What the agent has asked us to start syncing from SAJ. The file is on the
    container's disk, so a redeploy wipes it unless DATA_REQUEST_PATH points at
    a mounted volume — read it before you redeploy."""
    from agent import ops_agent
    reqs = ops_agent.data_requests()
    return {"count": len(reqs), "path": ops_agent.DATA_REQUEST_PATH,
            "requests": reqs}


@app.post("/agent/ask")
async def agent_ask(
    q: str = Query(..., min_length=1, description="question about the fleet, in English"),
    session: str | None = Query(None, description="session_id from a previous turn"),
    token: str | None = Query(None),
    x_trigger_token: str | None = Header(None),
):
    _check_auth(token or x_trigger_token)
    try:
        from agent import ops_agent
    except ImportError as e:  # noqa: BLE001
        raise HTTPException(501, f"agent not installed: {e}")
    return await ops_agent.ask(q, resume=session)


@app.on_event("startup")
def _resume_backfill():
    """A job still marked 'running' means the process was killed mid-copy
    (redeploy/crash) — pick it back up automatically."""
    backfill.resume_if_interrupted()
