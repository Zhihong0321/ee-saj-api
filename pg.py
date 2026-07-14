"""Postgres access layer for prod_main — works two ways, decided at runtime:

1. DATABASE_URL set  ->  direct psycopg2 connection  (use this on Railway PROD)
2. otherwise         ->  Railway HTTP SQL proxy       (local dev / fallback)

Both paths speak the same little interface:
    pg.run(sql, params) -> {"rows": [ {..}, .. ]}      (or {"error": .., "detail": ..})
    pg.upsert(table, cols, rows, conflict, update_cols)

SQL is written with $1,$2 placeholders (proxy style). The direct path rewrites
them to psycopg2's %s automatically, so callers never care which backend is live.

No secrets live in this file. Configure via env:
    DATABASE_URL      full postgres:// URL (Railway: reference the Postgres service)
    PG_PROXY_URL      proxy base (default: the known prod proxy)
    PG_PROXY_TOKEN    bearer token for the proxy (only needed when DATABASE_URL unset)
"""
from __future__ import annotations

import os
import re
import json
import urllib.request
import urllib.error

DATABASE_URL = os.environ.get("DATABASE_URL")
PROXY_URL = os.environ.get("PG_PROXY_URL", "https://pg-proxy-production.up.railway.app/api/sql")
PROXY_TOKEN = os.environ.get("PG_PROXY_TOKEN")
DB_NAME = os.environ.get("PG_DB_NAME", "prod_main")

_PLACEHOLDER = re.compile(r"\$(\d+)")


def _to_psycopg(sql: str, params):
    """Rewrite $1,$2,.. -> %s, reordering params to match occurrence order.

    Handles any ordering and repeated placeholders, so it is safe for every
    query in this project regardless of how the $n tokens appear.
    """
    order: list[int] = []

    def repl(m):
        order.append(int(m.group(1)))
        return "%s"

    new_sql = _PLACEHOLDER.sub(repl, sql)
    new_params = [params[i - 1] for i in order] if params else []
    return new_sql, new_params


# ---- direct psycopg2 path (Railway PROD) ---------------------------------
_conn = None


def _get_conn():
    global _conn
    import psycopg2
    if _conn is not None and _conn.closed == 0:
        return _conn
    _conn = psycopg2.connect(DATABASE_URL)
    _conn.autocommit = True
    return _conn


def _run_direct(sql, params):
    import psycopg2
    from psycopg2.extras import RealDictCursor
    for attempt in (1, 2):  # one reconnect on a dropped connection
        try:
            conn = _get_conn()
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params or [])
                if cur.description:
                    return {"rows": [dict(r) for r in cur.fetchall()]}
                return {"rows": [], "rowcount": cur.rowcount}
        except psycopg2.OperationalError as e:
            global _conn
            _conn = None
            if attempt == 2:
                return {"error": "operational", "detail": str(e)}
        except Exception as e:  # noqa: BLE001 — surface as the shared error shape
            return {"error": "query", "detail": str(e)}


# ---- HTTP proxy path (local / fallback) ----------------------------------
def _run_proxy(sql, params):
    if not PROXY_TOKEN:
        return {"error": "config",
                "detail": "neither DATABASE_URL nor PG_PROXY_TOKEN is set"}
    body = json.dumps({"db_name": DB_NAME, "sql": sql, "params": params or []}).encode()
    req = urllib.request.Request(
        PROXY_URL,
        data=body,
        headers={"Authorization": f"Bearer {PROXY_TOKEN}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": e.read().decode(errors="replace")}
    except Exception as e:  # noqa: BLE001
        return {"error": "proxy", "detail": str(e)}


# ---- public interface -----------------------------------------------------
def backend() -> str:
    return "database_url" if DATABASE_URL else "proxy"


def run(sql, params=None):
    if DATABASE_URL:
        new_sql, new_params = _to_psycopg(sql, params)
        return _run_direct(new_sql, new_params)
    return _run_proxy(sql, params)


def upsert(table, cols, rows, conflict, update_cols=None, batch=200):
    """Chunked multi-row INSERT ... ON CONFLICT. Returns rows sent."""
    if not rows:
        return 0
    n = len(cols)
    total = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        ph = ",".join("(" + ",".join(f"${j * n + k + 1}" for k in range(n)) + ")"
                      for j in range(len(chunk)))
        params = [v for row in chunk for v in row]
        if update_cols:
            action = "do update set " + ",".join(f"{c}=excluded.{c}" for c in update_cols)
        else:
            action = "do nothing"
        sql = (f"insert into {table} ({','.join(cols)}) values {ph} "
               f"on conflict ({conflict}) {action}")
        r = run(sql, params)
        if isinstance(r, dict) and "error" in r:
            raise RuntimeError(f"upsert {table} failed: {r}")
        total += len(chunk)
    return total


if __name__ == "__main__":
    import sys
    sql = sys.argv[1] if len(sys.argv) > 1 else "select now() as now"
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else []
    print(f"[backend: {backend()}]")
    print(json.dumps(run(sql, params), indent=2, default=str))
