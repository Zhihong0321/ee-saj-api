# PROTOTYPE — throwaway. Hardcoded, no error handling. Do not ship.
"""SAJ ops agent — ask a question in English, get an answer from prod saj_* tables.

Claude Agent SDK. One tool: read-only SQL. Nothing else is exposed, so the
worst it can do is run a slow SELECT.

    python -m agent.ops_agent "which inverters produced nothing yesterday?"

Needs in env: PG_PROXY_TOKEN (or DATABASE_URL) + Anthropic auth.
"""
from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
import json
import os
import sys

import pg
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

# Whatever ANTHROPIC_BASE_URL points at has to actually serve this model.
MODEL = os.environ.get("AGENT_MODEL", "claude-opus-5")
MAX_ROWS = 200

# Where the agent parks "we need this from SAJ but don't sync it" notes.
# NOTE: Railway's filesystem is ephemeral — this file is wiped on every
# redeploy unless DATA_REQUEST_PATH points at a mounted volume.
DATA_REQUEST_PATH = os.environ.get("DATA_REQUEST_PATH", "data_request.json")

SCHEMA = """
saj_device(device_sn PK, plant_uid, alias, model, rated_power_kw, phase_name,
           firmware, image_url, last_seen, updated_at)
saj_plant(plant_uid PK, plant_name, owner_name, installer_name, pv_power_wp,
          running_state, type_name, customer_id)
saj_customer_device_map(customer_id, device_sn, plant_uid, match_method,
                        confidence, verified)
saj_reading(device_sn, ts timestamptz, ac_power_w, pv_power_w, today_kwh,
            month_kwh, year_kwh, total_kwh, device_temp)      -- 5-min cadence, ~15M rows
            PRIMARY KEY (device_sn, ts)

Row counts are NOT given here on purpose — they change daily. Never state a
fleet size, plant count or row count from memory; SELECT it.
"""

SYSTEM = f"""You are the operations analyst for an EPC that monitors a fleet of SAJ solar
inverters. Answer questions about the fleet by querying Postgres.

WHO YOU ARE
You run inside the Claude Code harness, which will tempt you to say you are
Claude made by Anthropic. You are not. You are the SAJ fleet ops agent, and the
model actually serving you is `{MODEL}`. If asked what you are, say exactly
that. Never claim to be a model or a vendor other than the one above.

Tables (prod, read-only):
{SCHEMA}

Rules that matter:
- ts is stored UTC. The fleet is in Malaysia — convert with `ts AT TIME ZONE 'Asia/Kuala_Lumpur'`
  for anything a human calls "a day". Never report a raw UTC timestamp as local time.
- `AT TIME ZONE 'Asia/Kuala_Lumpur'` ALREADY returns Malaysia local time. Report that
  value as-is. Do NOT add 8 hours to it — doing so is an 8-hour error. Only add 8 hours
  to a timestamp you selected raw (no AT TIME ZONE in the query that produced it).
- Daily generation for a device-day is max(today_kwh) for that local day, NOT sum(ac_power_w).
- A device is "offline" if it has no reading in the last 24h. Check against
  max(ts) of the whole table, not now() — the nightly sweep may not have run.
  Count offline over ALL rows of saj_device, including devices that have never
  produced a reading at all. Do not silently narrow it to "devices that have
  reported before" — newly registered inverters that never came online are the
  ones worth knowing about. If you do split them out, give the total first.
- saj_reading is 15M rows. Always bound queries by device_sn and/or a ts range.

WHAT YOU ARE ACTUALLY READING
Every number you give comes from OUR Postgres mirror of the fleet — never from
the SAJ portal live. You have no connection to SAJ. If the mirror is stale or
incomplete, your answer is stale or incomplete; say so rather than presenting it
as the current state of the hardware.

CHECK FRESHNESS BEFORE ANY ANALYSIS
Readings arrive on a ~5-minute cadence, and a nightly sweep completes the day at
23:00 MYT. Before analysis work, check `select max(ts) from saj_reading`, and for
a specific device or day check that the rows you need are actually there.
If the data is behind — the newest reading is hours old during daylight, or the
day you were asked about has gaps or missing devices — still give the answer the
stored data supports, then say plainly that it is based on incomplete data and
tell them to sync first:
  - one plant, today .... POST /fetch/plant/{{plant_uid}}?days=1
  - one device, today ... POST /fetch/device/{{device_sn}}?days=1
  - whole fleet, today .. POST /sync/all
  - historical days ..... the /backfill page
Never silently report a partial day as if it were complete.

DATA WE DO NOT SYNC
saj_reading carries only ac_power_w, pv_power_w, today/month/year/total kwh and
device_temp. SAJ's raw feed has ~329 fields per row, so most of it is not here.
Not stored, and therefore NOT answerable from our DB: per-string DC (pv1..pv6
voltage / current / power, per-string currents), per-phase grid voltage, current
and frequency, inverter internal channel temperatures, module signal strength,
power factor, battery data, and any alarm or fault records.
If a technician needs one of those for O&M, do not guess, approximate, or infer
it from what we do have. Call `request_data` with what is needed and why, then
tell them it has been logged for review. One call per distinct field.

Answer with the actual numbers and device serials. Lead with the answer, then
the supporting detail. Do not describe the SQL you ran unless asked.
"""


@tool("sql", "Run one read-only SQL SELECT against the prod saj_* tables.", {"sql": str})
async def sql(args):
    q = args["sql"].strip().rstrip(";")
    low = q.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return {"content": [{"type": "text", "text": "ERROR: SELECT/WITH only."}]}
    if ";" in q:
        return {"content": [{"type": "text", "text": "ERROR: one statement only."}]}

    print(f"  [sql] {' '.join(q.split())[:150]}", flush=True)
    SQL_LOG.get().append(q)
    res = pg.run(q)
    if isinstance(res, dict) and "error" in res:
        return {"content": [{"type": "text", "text": f"ERROR: {res}"}]}
    rows = res.get("rows", [])
    print(f"  [sql] -> {len(rows)} rows", flush=True)
    return {"content": [{"type": "text",
                         "text": json.dumps(rows[:MAX_ROWS], default=str)}]}


@tool("request_data",
      "Log a request for SAJ data we do not currently sync into our DB. Use when "
      "a technician needs a field for O&M that saj_reading does not carry.",
      {"needed": str, "reason": str, "devices": str})
async def request_data(args):
    entry = {
        "requested_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "needed": args["needed"],
        "reason": args["reason"],
        "devices": args.get("devices", ""),
    }
    try:
        with open(DATA_REQUEST_PATH, encoding="utf-8") as f:
            reqs = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        reqs = []
    reqs.append(entry)
    with open(DATA_REQUEST_PATH, "w", encoding="utf-8") as f:
        json.dump(reqs, f, indent=2, ensure_ascii=False)
    print(f"  [request_data] {entry['needed'][:80]}", flush=True)
    return {"content": [{"type": "text",
                         "text": f"Logged as request #{len(reqs)} in {DATA_REQUEST_PATH}."}]}


def data_requests() -> list:
    """Everything the agent has asked us to start syncing."""
    try:
        with open(DATA_REQUEST_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


# Per-request, not global: two browser tabs chatting at once would otherwise
# each see the other's SQL.
SQL_LOG: contextvars.ContextVar[list[str]] = contextvars.ContextVar("sql_log")

_server = create_sdk_mcp_server(name="saj", version="0.1.0",
                                tools=[sql, request_data])


async def ask(question: str, resume: str | None = None) -> dict:
    """One chat turn. Pass back the returned session_id to continue the thread."""
    SQL_LOG.set([])
    answer, cost, session = "", 0.0, resume
    served_by = ""            # what the API says actually ran, not what we asked for
    options = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=SYSTEM,
        mcp_servers={"saj": _server},
        # Allowlist only — no bypassPermissions. The bundled CLI refuses that
        # flag when running as root, which is exactly how Railway starts us.
        allowed_tools=["mcp__saj__sql", "mcp__saj__request_data"],
        # This endpoint is internet-facing. The allowlist already gates them,
        # but name the dangerous built-ins explicitly so a future options change
        # can't quietly hand a public chat box a shell.
        disallowed_tools=["Bash", "Write", "Edit", "Read", "Task",
                          "WebFetch", "WebSearch"],
        max_turns=20,
        resume=resume,
    )
    async for msg in query(prompt=question, options=options):
        if isinstance(msg, AssistantMessage):
            served_by = getattr(msg, "model", "") or served_by
            for block in msg.content:
                if isinstance(block, TextBlock):
                    answer = block.text
        elif isinstance(msg, ResultMessage):
            cost = msg.total_cost_usd or 0.0
            session = msg.session_id
    # cost_usd is what the SDK *thinks* it cost at Anthropic list prices. On any
    # other provider it is fiction, so it is not shown in the UI.
    return {"answer": answer, "sql": SQL_LOG.get(), "cost_usd": round(cost, 4),
            "model": served_by or MODEL, "session_id": session}


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "How many inverters are in the fleet and when was the last reading?"
    print(f"Q: {q}\n")
    out = asyncio.run(ask(q))
    print(f"\nA: {out['answer']}\n")
    print(f"[{len(out['sql'])} queries, ${out['cost_usd']}]")
