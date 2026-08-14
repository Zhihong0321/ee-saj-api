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

SCHEMA = """
saj_device(device_sn PK, plant_uid, alias, model, rated_power_kw, phase_name,
           firmware, image_url, last_seen, updated_at)        -- 1,010 inverters
saj_plant(plant_uid PK, plant_name, owner_name, installer_name, pv_power_wp,
          running_state, type_name, customer_id)              -- 857 plants
saj_customer_device_map(customer_id, device_sn, plant_uid, match_method,
                        confidence, verified)                 -- 620 rows
saj_reading(device_sn, ts timestamptz, ac_power_w, pv_power_w, today_kwh,
            month_kwh, year_kwh, total_kwh, device_temp)      -- 15.2M rows, 5-min cadence
            PRIMARY KEY (device_sn, ts)
"""

SYSTEM = f"""You are the operations analyst for an EPC that monitors 1,010 SAJ solar
inverters. Answer questions about the fleet by querying Postgres.

Tables (prod, read-only):
{SCHEMA}

Rules that matter:
- ts is stored UTC. The fleet is in Malaysia — convert with `ts AT TIME ZONE 'Asia/Kuala_Lumpur'`
  for anything a human calls "a day". Never report a raw UTC timestamp as local time.
- Daily generation for a device-day is max(today_kwh) for that local day, NOT sum(ac_power_w).
- A device is "offline" if it has no reading in the last 24h. Check against
  max(ts) of the whole table, not now() — the nightly sweep may not have run.
- saj_reading is 15M rows. Always bound queries by device_sn and/or a ts range.

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


# Per-request, not global: two browser tabs chatting at once would otherwise
# each see the other's SQL.
SQL_LOG: contextvars.ContextVar[list[str]] = contextvars.ContextVar("sql_log")

_server = create_sdk_mcp_server(name="saj", version="0.1.0", tools=[sql])


async def ask(question: str, resume: str | None = None) -> dict:
    """One chat turn. Pass back the returned session_id to continue the thread."""
    SQL_LOG.set([])
    answer, cost, session = "", 0.0, resume
    options = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=SYSTEM,
        mcp_servers={"saj": _server},
        # Allowlist only — no bypassPermissions. The bundled CLI refuses that
        # flag when running as root, which is exactly how Railway starts us.
        allowed_tools=["mcp__saj__sql"],
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
            for block in msg.content:
                if isinstance(block, TextBlock):
                    answer = block.text
        elif isinstance(msg, ResultMessage):
            cost = msg.total_cost_usd or 0.0
            session = msg.session_id
    return {"answer": answer, "sql": SQL_LOG.get(), "cost_usd": round(cost, 4),
            "session_id": session}


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "How many inverters are in the fleet and when was the last reading?"
    print(f"Q: {q}\n")
    out = asyncio.run(ask(q))
    print(f"\nA: {out['answer']}\n")
    print(f"[{len(out['sql'])} queries, ${out['cost_usd']}]")
