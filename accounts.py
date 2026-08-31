"""SAJ portal accounts, stored in the DB instead of Railway env vars.

One row per SAJ monitoring login (operation01, operation02, ...). This is the
single source of truth for *which* credentials the service uses — the web
client, the nightly sweep, the fast sync and the history backfill all read from
here now. Railway's SAJ_USER / SAJ_PASS are only a one-time seed: on first boot
against an empty table we copy them in so nothing breaks mid-migration, and
after that you manage everything from the /accounts page. Change a password
there when SAJ rotates it — no redeploy, no env var to touch.

Model:
  * exactly one account is `is_primary` — that's the one the app, the nightly
    sweep and the fast sync log in as (the portal is single-session).
  * every `active` account forms the backfill pool (backfill parallelises across
    accounts, so more active logins = faster history copy).

Passwords are stored as plain text, same as the existing credential vault — this
DB is already the trusted prod store and the read path is org-internal. The list
API never returns the raw password; edits leave it blank to keep the current one.
"""
from __future__ import annotations

import os

import pg
from saj_api import SajClient, SajError

DEFAULT_ORG = "OAhz"


def _db(sql, params=None):
    r = pg.run(sql, params)
    if isinstance(r, dict) and "error" in r:
        raise RuntimeError(f"accounts db: {r}")
    return r


def ensure_schema() -> None:
    _db("""create table if not exists saj_account (
             username    text primary key,
             password    text not null,
             org_code    text default 'OAhz',
             active      boolean not null default true,
             is_primary  boolean not null default false,
             remarks     text,
             last_ok_at  timestamptz,
             last_error  text,
             updated_at  timestamptz not null default now(),
             created_at  timestamptz not null default now())""")
    _db("create unique index if not exists saj_account_one_primary "
        "on saj_account (is_primary) where is_primary")


# ---- reads (credential resolution) ---------------------------------------
def get_primary() -> dict | None:
    """The account the web client / nightly sweep / fast sync log in as."""
    rows = _db("select username, password, org_code from saj_account "
               "where is_primary and active limit 1").get("rows") or []
    return rows[0] if rows else None


def get_pool() -> list[dict]:
    """Every active account — the parallel pool the history backfill spreads over."""
    return _db("select username, password, org_code from saj_account "
               "where active order by is_primary desc, username").get("rows") or []


def list_accounts() -> list[dict]:
    """Everything the management page shows — never the raw password."""
    rows = _db("""select username, org_code, active, is_primary, remarks,
                    last_ok_at, last_error, updated_at,
                    (password is not null and password <> '') as has_password
                  from saj_account order by is_primary desc, username""").get("rows") or []
    return rows


# ---- writes ---------------------------------------------------------------
def upsert(username: str, password: str | None, org_code: str | None = None,
           active: bool = True, is_primary: bool = False,
           remarks: str | None = None) -> dict:
    """Add or update one account.

    A blank/None `password` on an existing account keeps the stored one — so the
    page can edit org/flags/remarks without re-typing the secret. Setting this
    account primary atomically demotes whoever held it before.
    """
    username = (username or "").strip()
    if not username:
        raise ValueError("username is required")
    exists = bool(_db("select 1 from saj_account where username=$1",
                      [username]).get("rows"))
    if not password and not exists:
        raise ValueError("password is required for a new account")
    org = (org_code or "").strip() or DEFAULT_ORG

    if is_primary:
        # Single-session portal: only one primary at a time.
        _db("update saj_account set is_primary=false, updated_at=now() "
            "where is_primary and username<>$1", [username])

    if exists:
        if password:
            _db("update saj_account set password=$2, org_code=$3, active=$4, "
                "is_primary=$5, remarks=$6, updated_at=now() where username=$1",
                [username, password, org, active, is_primary, remarks])
        else:
            _db("update saj_account set org_code=$2, active=$3, is_primary=$4, "
                "remarks=$5, updated_at=now() where username=$1",
                [username, org, active, is_primary, remarks])
    else:
        _db("insert into saj_account (username, password, org_code, active, "
            "is_primary, remarks) values ($1,$2,$3,$4,$5,$6)",
            [username, password, org, active, is_primary, remarks])
    return {"username": username, "primary": is_primary, "active": active}


def set_primary(username: str) -> None:
    _db("update saj_account set is_primary=false, updated_at=now() where is_primary")
    n = _db("update saj_account set is_primary=true, active=true, updated_at=now() "
            "where username=$1", [username]).get("rowcount")
    if not n:
        raise ValueError(f"no such account: {username}")


def set_active(username: str, active: bool) -> None:
    _db("update saj_account set active=$2, updated_at=now() where username=$1",
        [username, active])


def delete(username: str) -> None:
    _db("delete from saj_account where username=$1", [username])


def record_result(username: str, ok: bool, error: str | None = None) -> None:
    """Best-effort stamp of the last login attempt, shown on the page."""
    try:
        if ok:
            _db("update saj_account set last_ok_at=now(), last_error=null, "
                "updated_at=now() where username=$1", [username])
        else:
            _db("update saj_account set last_error=$2, updated_at=now() "
                "where username=$1", [username, (error or "")[:400]])
    except Exception:  # noqa: BLE001 — never let telemetry break the caller
        pass


def test_login(username: str) -> dict:
    """Try to log this account into the SAJ portal; record and return the result."""
    rows = _db("select username, password, org_code from saj_account "
               "where username=$1", [username]).get("rows") or []
    if not rows:
        raise ValueError(f"no such account: {username}")
    a = rows[0]
    c = SajClient(username=a["username"], password=a["password"],
                  org_code=a.get("org_code") or DEFAULT_ORG)
    try:
        c.login(a["username"], a["password"])
        record_result(username, True)
        return {"username": username, "ok": True, "org_code": c.org_code}
    except SajError as e:
        record_result(username, False, f"{e.err_code}: {e.err_msg}")
        return {"username": username, "ok": False,
                "err_code": e.err_code, "error": e.err_msg}
    except Exception as e:  # noqa: BLE001
        record_result(username, False, str(e))
        return {"username": username, "ok": False, "error": str(e)}


# ---- one-time migration off env vars -------------------------------------
def seed_from_env_if_empty() -> int:
    """First-boot migration: if the table is empty, copy the Railway env creds in.

    SAJ_USER/SAJ_PASS -> the primary account; BACKFILL_USERS (sharing
    BACKFILL_PASS or SAJ_PASS) -> the active pool. After this the env vars are
    dead weight — accounts live in the DB. Returns how many rows were seeded.
    """
    ensure_schema()
    if _db("select 1 from saj_account limit 1").get("rows"):
        return 0
    seeded = 0
    primary_user = os.environ.get("SAJ_USER")
    primary_pass = os.environ.get("SAJ_PASS")
    if primary_user and primary_pass:
        upsert(primary_user, primary_pass, active=True, is_primary=True,
               remarks="seeded from SAJ_USER/SAJ_PASS")
        seeded += 1
    backfill_pass = os.environ.get("BACKFILL_PASS") or primary_pass
    backfill_users = [u.strip() for u in
                      os.environ.get("BACKFILL_USERS", "").split(",") if u.strip()]
    for u in backfill_users:
        if u == primary_user or not backfill_pass:
            continue
        try:
            upsert(u, backfill_pass, active=True, is_primary=False,
                   remarks="seeded from BACKFILL_USERS")
            seeded += 1
        except Exception:  # noqa: BLE001
            pass
    return seeded
