# ee-saj-api — SAJ Data Fetcher

HTTP service that pulls the 5-minute generation feed from the SAJ / elekeeper
portal (`iop.saj-electric.com`) into the prod `saj_reading` table. Deployed on
Railway. It is the **write side** of the pipeline — the client app only reads
`saj_reading` back out.

You trigger a fetch by **device serial** or **plant UID**; the service logs into
the SAJ portal (auto-relogin on token expiry), pulls the raw 5-min rows, and
upserts them into Postgres.

**Live:** https://ee-saj-api-production.up.railway.app · interactive docs at
[`/docs`](https://ee-saj-api-production.up.railway.app/docs).

## Endpoints

| Method | Path | What it does |
|---|---|---|
| `POST` | `/fetch/plant/{plant_uid}?days=1` | **Main app call** — refresh the plant into prod, return chart-ready `series` + `daily` |
| `POST` | `/fetch/device/{device_sn}?days=1` | Same for one inverter serial (adds `latest`) |
| `GET`  | `/plant/{plant_uid}/series?days=1` | Chart data straight from prod — no SAJ call, no token |
| `GET`  | `/device/{device_sn}/series?days=1` | Same, one inverter |
| `GET`  | `/device/{device_sn}/info` | Inverter model / rated kW / phase / firmware / image (DB-cached; self-populates once) |
| `GET`  | `/device/{device_sn}/latest` | Newest stored reading — confirm a trigger landed |
| `GET`  | `/health` | Liveness + which DB backend + whether protected |

`days=1` = today only (the per-visit trigger). Larger `days` backfills history in
one shot (capped by `MAX_DAYS`). `/fetch/*` require the `X-Trigger-Token` header;
`?series=false` skips the chart payload; `?force=true` bypasses the freshness gate.

**Response shape** (`/fetch/plant` and `/series`): `series` = `[{ts, ac_power_w}]`
(ts is UTC — display +8h as Asia/Kuala_Lumpur), `daily` = `[{day, kwh}]`.

### Trigger examples

```bash
# by device serial
curl -X POST "https://<your-app>.up.railway.app/fetch/device/R6M2063J2516E18728?days=1" \
     -H "X-Trigger-Token: $TRIGGER_TOKEN"

# by plant UID (fetches every inverter in that plant)
curl -X POST "https://<your-app>.up.railway.app/fetch/plant/<PLANT_UID>?days=1" \
     -H "X-Trigger-Token: $TRIGGER_TOKEN"

# confirm what landed
curl "https://<your-app>.up.railway.app/device/R6M2063J2516E18728/latest"
```

If `TRIGGER_TOKEN` is unset the token header is not required (fine for a first
smoke test) — set it before real use.

## Deploy to Railway

1. **New Project → Deploy from GitHub repo** → pick `Zhihong0321/ee-saj-api`.
   Railway auto-detects Python (Nixpacks) and runs `uvicorn main:app` (see
   `Procfile` / `railway.json`).
2. Add the Postgres that holds `saj_reading` to the same project (or use the
   existing one), then set the service **Variables**:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (reference your Postgres service) |
   | `SAJ_USER` | `operation01` |
   | `SAJ_PASS` | *(the account password)* |
   | `TRIGGER_TOKEN` | *(a long random string you choose)* |

   If you cannot set `DATABASE_URL`, the service falls back to the HTTP SQL proxy
   — set `PG_PROXY_TOKEN` (and optionally `PG_PROXY_URL`) instead. Note the proxy
   token is short-lived and expires, so `DATABASE_URL` is strongly preferred for
   prod.
3. Ensure the `saj_*` tables exist (`schema_prod.sql`). They already exist in the
   current prod DB; the file is here for a fresh environment.
4. Deploy. Hit `/health` to confirm `db_backend: database_url`.

## Two operating modes

**Daytime — sync per visit (on-demand).** When a customer opens the app, have the
app POST `/fetch/plant/{uid}` (or `/fetch/device/{sn}`). That pulls today's data
(this morning → now) into `saj_reading`, then the app reads it back. A freshness
gate (`VISIT_FRESH_SECONDS`, default 240s) means if the same device was already
pulled seconds ago, the call serves cache and skips SAJ — so rapid re-opens don't
hammer the portal (data is only 5-min cadence). Add `?force=true` to override.

**Nightly — full sweep at 23:00 MYT.** `sync_all.py` fetches today's complete
curve for **every** device (always a full pull, no gate), guaranteeing a gap-free
day once the sun is down. Set it up as a **Railway Cron**:

1. In the same Railway project, **New → GitHub Repo → same repo** (creates a 2nd
   service off `ee-saj-api`).
2. That service's **Settings**:
   - **Start Command:** `python sync_all.py`
   - **Cron Schedule:** `0 15 * * *`  → 15:00 UTC = **23:00 Malaysia time**
3. **Variables:** same `DATABASE_URL`, `SAJ_USER`, `SAJ_PASS` as the web service
   (Railway "shared variables" or copy them). `TRIGGER_TOKEN` is not needed here.

Railway runs the start command on the schedule; the script sweeps the fleet
(~1010 devices at ~1 req/s ≈ 20 min), logs progress, and exits. Tunables:
`SAJ_REQ_INTERVAL` (throttle), `SYNC_DAYS` (days back, default 1), `SYNC_LIMIT`
(cap for testing).

## Config (env vars)

See `.env.example`. Nothing secret is committed — all credentials come from
Railway Variables.

## Local dev

```bash
pip install -r requirements.txt
cp .env.example .env    # fill in SAJ_PASS and either DATABASE_URL or PG_PROXY_TOKEN
# load .env into your shell, then:
uvicorn main:app --reload
```

## Notes

- **Single session:** the SAJ account allows one active login; logging in
  elsewhere invalidates the token. Use a **dedicated monitoring account** that no
  human logs into interactively. All portal calls here are serialised behind one
  shared client + lock.
- **Timezone:** the portal returns Malaysia-local timestamps; they are tagged
  `+08:00` on insert so Postgres stores the true UTC instant. The reader shifts
  back to `Asia/Kuala_Lumpur` for display.
- The SAJ request-signing secret in `saj_api.py` is a public value shipped in the
  portal's JS bundle — not a real credential. The actual account password is
  never in this repo.
