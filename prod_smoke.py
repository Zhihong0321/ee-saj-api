"""Revision-aware end-to-end smoke test for the deployed production API.

The test waits until Railway serves the expected commit, then force-refreshes one
real inverter for today plus yesterday through the public production endpoint.
That idempotently upserts production readings and verifies the database readback.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

DEFAULT_BASE_URL = "https://ee-saj-api-production.up.railway.app"
DEFAULT_DEVICE_SN = "R6M2053J2623E08431"
MYT = dt.timezone(dt.timedelta(hours=8))


def _check(checks: list[dict], name: str, ok: bool, detail: str) -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def redact(text: str, secrets: list[str]) -> str:
    for secret in sorted((item for item in secrets if item), key=len,
                         reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text


def _request_json(session: requests.Session, method: str, url: str,
                  timeout: float, **kwargs: Any) -> tuple[dict, float]:
    started = time.monotonic()
    response = session.request(method, url, timeout=timeout, **kwargs)
    elapsed = time.monotonic() - started
    if response.status_code >= 400:
        body = response.text[:500].replace("\n", " ")
        raise RuntimeError(f"{method} {url} returned HTTP "
                           f"{response.status_code}: {body}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{method} {url} returned non-JSON content") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{method} {url} returned a non-object JSON response")
    return payload, elapsed


def wait_for_revision(session: requests.Session, base_url: str,
                      expected_revision: str, deploy_timeout: float,
                      request_timeout: float, poll_seconds: float) -> tuple[dict, float]:
    """Poll health until production is serving the exact pushed revision."""
    started = time.monotonic()
    deadline = started + deploy_timeout
    last_state = "no response"
    while time.monotonic() < deadline:
        try:
            health, _ = _request_json(session, "GET", f"{base_url}/health",
                                      request_timeout)
            actual = str(health.get("revision") or "")
            last_state = f"revision={actual or 'missing'}"
            if not expected_revision or actual == expected_revision:
                return health, time.monotonic() - started
        except (requests.RequestException, RuntimeError) as exc:
            last_state = str(exc)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            print(f"[WAIT] production deployment: {last_state}", flush=True)
            time.sleep(min(poll_seconds, remaining))
    raise RuntimeError("production did not reach expected revision "
                       f"{expected_revision} within {deploy_timeout:.0f}s; "
                       f"last state: {last_state}")


def validate_health(health: dict, expected_revision: str) -> list[dict]:
    checks: list[dict] = []
    _check(checks, "health_ok", health.get("ok") is True,
           f"ok={health.get('ok')!r}")
    _check(checks, "service_identity", health.get("service") == "ee-saj-api",
           f"service={health.get('service')!r}")
    _check(checks, "deployed_revision",
           not expected_revision or health.get("revision") == expected_revision,
           f"actual={health.get('revision')!r} expected={expected_revision!r}")
    _check(checks, "production_database",
           health.get("db_backend") == "database_url",
           f"db_backend={health.get('db_backend')!r}")
    _check(checks, "production_auth_enabled", health.get("protected") is True,
           f"protected={health.get('protected')!r}")
    return checks


def validate_data_response(payload: dict, device_sn: str, days: int,
                           yesterday: str) -> list[dict]:
    checks: list[dict] = []
    _check(checks, "target_device", payload.get("device_sn") == device_sn,
           f"device_sn={payload.get('device_sn')!r}")
    _check(checks, "requested_window", payload.get("days") == days,
           f"days={payload.get('days')!r}")
    _check(checks, "live_saj_pull", payload.get("source") == "live",
           f"source={payload.get('source')!r}")
    rows_written = payload.get("rows_written")
    _check(checks, "production_rows_written",
           isinstance(rows_written, int) and rows_written > 0,
           f"rows_written={rows_written!r}")

    latest = payload.get("latest")
    _check(checks, "latest_reading_present",
           isinstance(latest, dict) and bool(latest.get("ts")),
           f"latest_ts={latest.get('ts') if isinstance(latest, dict) else None!r}")

    series = payload.get("series") if isinstance(payload.get("series"), list) else []
    stamps = [str(item.get("ts")) for item in series if item.get("ts")]
    _check(checks, "power_series_present", bool(stamps),
           f"series_points={len(stamps)}")
    _check(checks, "power_series_sorted", stamps == sorted(stamps),
           "series timestamps are chronological")

    daily = payload.get("daily") if isinstance(payload.get("daily"), list) else []
    day_values = {str(item.get("day"))[:10]: _number(item.get("kwh"))
                  for item in daily if item.get("day")}
    _check(checks, "completed_day_persisted",
           yesterday in day_values and day_values[yesterday] is not None,
           f"expected_day={yesterday} available_days={sorted(day_values)}")
    return checks


def run_smoke(base_url: str, device_sn: str, trigger_token: str,
              expected_revision: str, days: int = 2,
              deploy_timeout: float = 900, request_timeout: float = 180,
              poll_seconds: float = 10,
              max_fetch_seconds: float = 180) -> dict:
    base_url = base_url.rstrip("/")
    session = requests.Session()
    session.headers.update({"Accept": "application/json",
                            "User-Agent": "ee-saj-api-prod-smoke/1.0"})
    health, deploy_wait = wait_for_revision(
        session, base_url, expected_revision, deploy_timeout,
        request_timeout, poll_seconds)
    checks = validate_health(health, expected_revision)

    yesterday = (dt.datetime.now(MYT).date() - dt.timedelta(days=1)).isoformat()
    fetch_payload, fetch_seconds = _request_json(
        session, "POST", f"{base_url}/fetch/device/{device_sn}",
        request_timeout,
        headers={"X-Trigger-Token": trigger_token},
        params={"days": days, "force": "true", "series": "true"})
    checks.extend(validate_data_response(fetch_payload, device_sn, days, yesterday))
    _check(checks, "fetch_performance", fetch_seconds <= max_fetch_seconds,
           f"elapsed={fetch_seconds:.2f}s limit={max_fetch_seconds:.2f}s")

    readback, readback_seconds = _request_json(
        session, "GET", f"{base_url}/device/{device_sn}/series",
        request_timeout, params={"days": days})
    readback_checks = validate_data_response(
        {**readback, "source": "live", "rows_written": 1},
        device_sn, days, yesterday)
    for item in readback_checks:
        if item["name"] in {"power_series_present", "power_series_sorted",
                           "completed_day_persisted", "target_device",
                           "requested_window"}:
            item["name"] = f"readback_{item['name']}"
            checks.append(item)

    latest_payload, latest_seconds = _request_json(
        session, "GET", f"{base_url}/device/{device_sn}/latest",
        request_timeout)
    latest = latest_payload.get("latest")
    _check(checks, "readback_latest_present",
           latest_payload.get("device_sn") == device_sn and
           isinstance(latest, dict) and bool(latest.get("ts")),
           f"latest_ts={latest.get('ts') if isinstance(latest, dict) else None!r}")

    return {
        "ok": all(item["ok"] for item in checks),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "target": "production", "base_url": base_url,
        "device_sn": device_sn, "days": days,
        "expected_revision": expected_revision,
        "actual_revision": health.get("revision"),
        "checks": checks,
        "timings_seconds": {
            "deploy_wait": round(deploy_wait, 3),
            "fetch": round(fetch_seconds, 3),
            "series_readback": round(readback_seconds, 3),
            "latest_readback": round(latest_seconds, 3),
        },
    }


def _write_report(path: str, report: dict) -> None:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    temporary.replace(target)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("PROD_BASE_URL",
                                                         DEFAULT_BASE_URL))
    parser.add_argument("--device-sn", default=os.getenv("PROD_SMOKE_DEVICE_SN",
                                                          DEFAULT_DEVICE_SN))
    parser.add_argument("--expected-revision",
                        default=os.getenv("EXPECTED_REVISION", ""))
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--deploy-timeout", type=float, default=900)
    parser.add_argument("--request-timeout", type=float, default=180)
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--max-fetch-seconds", type=float, default=180)
    parser.add_argument("--json-out", default=".debug/prod-smoke-report.json")
    return parser


def main() -> int:
    args = _parser().parse_args()
    trigger_token = os.getenv("PROD_TRIGGER_TOKEN") or os.getenv("TRIGGER_TOKEN")
    secrets = [trigger_token or ""]
    try:
        if not args.expected_revision:
            raise RuntimeError("--expected-revision is required")
        if not trigger_token:
            raise RuntimeError("PROD_TRIGGER_TOKEN is not set")
        report = run_smoke(
            args.base_url, args.device_sn, trigger_token,
            args.expected_revision, days=args.days,
            deploy_timeout=args.deploy_timeout,
            request_timeout=args.request_timeout,
            poll_seconds=args.poll_seconds,
            max_fetch_seconds=args.max_fetch_seconds)
    except Exception as exc:
        safe_error = redact(str(exc), secrets)
        report = {
            "ok": False,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "target": "production", "base_url": args.base_url,
            "device_sn": args.device_sn,
            "expected_revision": args.expected_revision,
            "checks": [{"name": "production_smoke", "ok": False,
                        "detail": safe_error}],
        }
    _write_report(args.json_out, report)
    for item in report["checks"]:
        print(f"[{'PASS' if item['ok'] else 'FAIL'}] {item['name']}: {item['detail']}")
    print("PRODUCTION RESULT:", "PASS" if report["ok"] else "FAIL")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
