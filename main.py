"""EE SAJ Data Fetcher — HTTP API on Railway.

Trigger a fetch of the 5-min generation feed from the SAJ portal into prod
`saj_reading`, keyed by device serial or plant UID.

    POST /fetch/device/{device_sn}?days=1     one inverter
    POST /fetch/plant/{plant_uid}?days=1       every inverter in a plant
    GET  /device/{device_sn}/latest            confirm what landed
    GET  /health                               liveness + config

Auth: if TRIGGER_TOKEN is set, every /fetch call must present it as
`?token=...` or header `X-Trigger-Token: ...`. If unset, fetch is open (fine for
a first smoke test; set it before real use).

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
from saj_api import SajClient, SajError

SAJ_USER = os.environ.get("SAJ_USER")
SAJ_PASS = os.environ.get("SAJ_PASS")
TRIGGER_TOKEN = os.environ.get("TRIGGER_TOKEN")
MAX_DAYS = int(os.environ.get("MAX_DAYS", "14"))

app = FastAPI(title="EE SAJ Data Fetcher", version="1.0.0")

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
        "time": dt.datetime.utcnow().isoformat() + "Z",
    }


@app.post("/fetch/device/{device_sn}")
def fetch_device(
    device_sn: str,
    days: int = Query(1, ge=1, le=MAX_DAYS, description="days of history back from today"),
    token: str | None = Query(None),
    x_trigger_token: str | None = Header(None),
):
    _check_auth(token or x_trigger_token)
    with _lock:
        client = _get_client()
        try:
            written = fetcher.fetch_device(client, device_sn, days=days)
        except SajError as e:
            raise HTTPException(502, f"SAJ error {e.err_code}: {e.err_msg}")
    return {
        "device_sn": device_sn,
        "days": days,
        "rows_written": written,
        "latest": fetcher.latest(device_sn),
    }


@app.post("/fetch/plant/{plant_uid}")
def fetch_plant(
    plant_uid: str,
    days: int = Query(1, ge=1, le=MAX_DAYS, description="days of history back from today"),
    token: str | None = Query(None),
    x_trigger_token: str | None = Header(None),
):
    _check_auth(token or x_trigger_token)
    with _lock:
        client = _get_client()
        try:
            per_device = fetcher.fetch_plant(client, plant_uid, days=days)
        except SajError as e:
            raise HTTPException(502, f"SAJ error {e.err_code}: {e.err_msg}")
    if not per_device:
        raise HTTPException(404, f"no devices found for plant {plant_uid}")
    return {
        "plant_uid": plant_uid,
        "days": days,
        "device_count": len(per_device),
        "rows_written": sum(per_device.values()),
        "devices": per_device,
    }


@app.get("/device/{device_sn}/latest")
def device_latest(device_sn: str):
    row = fetcher.latest(device_sn)
    return {"device_sn": device_sn, "latest": row}
