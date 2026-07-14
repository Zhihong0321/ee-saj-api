"""EE SAJ Data Fetcher — HTTP API on Railway.

Trigger a fetch of the 5-min generation feed from the SAJ portal into prod
`saj_reading`, keyed by device serial or plant UID.

    POST /fetch/device/{device_sn}?days=1[&force=true]   one inverter
    POST /fetch/plant/{plant_uid}?days=1[&force=true]     every inverter in a plant
    GET  /device/{device_sn}/latest                       confirm what landed
    GET  /health                                          liveness + config

Two intended use modes:
  * Daytime, per-visit: the app POSTs /fetch/plant/{uid} when a customer opens
    their dashboard -> today's data (morning -> now) is pulled into the DB.
    A freshness gate (VISIT_FRESH_SECONDS) skips the SAJ call when the stored
    data is already fresh, so rapid re-opens don't hammer the portal.
  * Nightly 23:00 MYT: a Railway Cron runs sync_all.py -> full sweep of every
    device for the complete day (always a full pull, no gate).

Auth: if TRIGGER_TOKEN is set, every /fetch call must present it as `?token=...`
or header `X-Trigger-Token: ...`.

The SAJ account is single-session, so all portal calls are serialised behind one
shared client + lock.
"""
from __future__ import annotations

import os
import threading
import datetime as dt

from fastapi import FastAPI, HTTPException, Header, Query

import fetcher
import pg
import r2
from saj_api import SajClient, SajError

SAJ_USER = os.environ.get("SAJ_USER")
SAJ_PASS = os.environ.get("SAJ_PASS")
TRIGGER_TOKEN = os.environ.get("TRIGGER_TOKEN")
MAX_DAYS = int(os.environ.get("MAX_DAYS", "14"))
# per-visit gate: skip the SAJ pull if today's newest stored reading is younger
# than this (data is 5-min cadence, so ~4 min avoids redundant pulls on refresh).
VISIT_FRESH_SECONDS = int(os.environ.get("VISIT_FRESH_SECONDS", "240"))

app = FastAPI(title="EE SAJ Data Fetcher", version="1.1.0")

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
        "db_backend": pg.backend(),
        "saj_account": SAJ_USER,
        "protected": bool(TRIGGER_TOKEN),
        "visit_fresh_seconds": VISIT_FRESH_SECONDS,
        "r2_image_mirror": r2.enabled(),
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


# ---- read-only: chart data straight from prod (no SAJ call) ---------------
@app.get("/device/{device_sn}/series")
def device_series(device_sn: str,
                  days: int = Query(1, ge=1, le=MAX_DAYS)):
    return {"device_sn": device_sn, "days": days,
            "series": fetcher.series_for_sns([device_sn], days),
            "daily": fetcher.daily_for_sns([device_sn], days)}


@app.get("/plant/{plant_uid}/series")
def plant_series(plant_uid: str,
                 days: int = Query(1, ge=1, le=MAX_DAYS)):
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
