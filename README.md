# ee-saj-api — SAJ Data Fetcher

HTTP service that pulls the 5-minute generation feed from the SAJ / elekeeper
portal (`iop.saj-electric.com`) into the prod `saj_reading` table. Deployed on
Railway. It is the **write side** of the pipeline — the client app only reads
`saj_reading` back out.

You trigger a fetch by **device serial** or **plant UID**; the service logs into
the SAJ portal (auto-relogin on token expiry), pulls the raw 5-min rows, and
upserts them into Postgres.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| `POST` | `/fetch/device/{device_sn}?days=1` | Fetch one inverter's last `days` days into `saj_reading` |
| `POST` | `/fetch/plant/{plant_uid}?days=1` | Resolve the plant's inverters (live) and fetch each |
| `GET`  | `/device/{device_sn}/latest` | Newest stored reading — confirm a trigger landed |
| `GET`  | `/health` | Liveness + which DB backend + whether protected |

`days=1` = today only (the freshness trigger). Larger `days` backfills history in
one shot (capped by `MAX_DAYS`, default 14).

Interactive docs at `/docs` once deployed.

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

### Scheduling (optional)

For automatic freshness, add a **Railway Cron** service (or an external cron / the
client app) that POSTs `/fetch/device/...` or `/fetch/plant/...` on an interval.
Recommended: fetch a plant only when a customer is actively viewing it (on-demand),
plus a nightly full backfill. 5-minute data means polling faster than ~15 min is
wasted work — and keeps you well under the portal's rate alarm.

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
