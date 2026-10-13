# Deploying Mishkah on Render (one free web service)

Phase 1 is research + paper trading only. The deployment below runs the Python worker, the panel API
and the Mission Control UI in a single container with a single origin, so there is no CORS, no
browser-side secret, and nothing to keep in sync between two hosts.

```
                       ┌────────────────────────────── Render web service (Docker) ─────────────────────────────┐
 browser ──https──▶    │  uvicorn (worker/worker/api/app.py)                                                │
 (Mission Control)     │   /                static Next.js export (ui/out)                                   │
                       │   /api/auth/*      panel password + session cookie (scrypt, HttpOnly)               │
                       │   /api/admin/*     settings writes → worker → audit_log (only writer)               │
                       │   /api/bundle/*    UI data built from the database, session-gated                   │
                       │   /webhook         signed fire-and-forget trigger for an external scheduler         │
                       │   /healthz         liveness + database + "is a password set"                        │
                       └───────────────┬─────────────────────────────────────────────────────────────────────┘
                                       │ psycopg (pooled)
                                 ┌─────▼─────┐
                                 │ Postgres  │  Neon / Supabase free tier
                                 └───────────┘
```

## 1. Database (5 minutes)

Create a free Postgres and copy the **pooled** connection string:

* [Neon](https://neon.tech) → project → *Connection string* (add `?sslmode=require` if it is not there),
* or Supabase → *Connect* → *Session pooler* URI.

Migrations run automatically on boot (`python -m worker db up`), and they are additive/idempotent, so
a re-deploy is safe.

## 2. Deploy

Option A — blueprint (recommended):

1. Render dashboard → **New → Blueprint** → select this repository (`render.yaml` at the root).
2. Fill the variable it asks for: **`DATABASE_URL`**.
3. Let Render generate **`MISHKAH_MASTER_KEY`** and **`WORKER_WEBHOOK_SECRET`** (both are already
   `generateValue: true` in the blueprint).

Option B — manual web service:

| setting | value |
| --- | --- |
| Environment | **Docker** |
| Dockerfile path | `./Dockerfile` |
| Health check path | `/healthz` |
| Instance type | Free |

Environment variables:

| variable | value | why |
| --- | --- | --- |
| `DATABASE_URL` | your pooled connection string | the only required variable |
| `MISHKAH_MASTER_KEY` | 32+ random bytes, base64 or raw | encrypts panel-managed API keys |
| `WORKER_WEBHOOK_SECRET` | random string | HMAC secret for `/webhook` |
| `GM_DEMO_MODE` | `0` | `1` serves the synthetic demo dataset instead of real sources |
| `PAPER_ONLY` | `true` | a `false` value is refused by the build (no order code exists) |
| `TZ` | `UTC` | every timestamp is UTC; Tehran boundaries come from `zoneinfo` |

**Back up `MISHKAH_MASTER_KEY`.** Keys stored from the panel are encrypted under it; losing it means
re-entering them (that is the design — a database dump alone must reveal nothing usable).

Once the health check turns green, open the service URL: the Mission Control panel loads. First run
asks you to **set a panel password**, then you are inside.

## 3. Configure the integrations from the panel (no redeploy, no secrets in the dashboard)

Open **Settings** in the left rail. Each group has a *test this group* button, so you can prove a
connection before relying on it:

| group | fields | what the test does |
| --- | --- | --- |
| **Interview model — Jev** | `JEV_PROVIDER`, `JEV_API_KEY`, `JEV_BASE_URL`, `JEV_MODEL`, `JEV_ALLOWED_HOSTS` | refuses a host outside the allowlist *before sending anything*; otherwise one real (or mock, in demo mode) `noul` question |
| **Extraction + narrative** | `OPENROUTER_API_KEY`, `EXTRACTION_MODELS`, `EXPLAINER_MODEL` | `GET /models` on the configured base URL |
| **Telegram ingestion** | `TELEGRAM_API_ID/HASH/SESSION/CHANNELS` | checks the library and that credentials are present (never sends anything — read-only) |
| **Price sources** | `NOBITEX_BASE`, `XAUUSD_PROVIDER`, `TGJU_ENABLED`, `TSETMC_ENABLED`, `NOBITEX_MAX_REQ_PER_MIN` | one shallow request to the Nobitex history endpoint |
| **Panel + worker** | `UI_ADMIN_PASSWORD`, `UI_SESSION_MINUTES`, `WORKER_WEBHOOK_SECRET`, `GM_DEMO_MODE` | reports the master-key source, whether a password is set, and active sessions |

How to read a row:

* `panel` — the value stored here, encrypted; it **overrides** the environment,
* `env` — coming from the Render environment (visible only as `name=value` in the dashboard),
* `unset` — nothing configured,
* a secret shows a **fingerprint** (`sha256:…` + the last four characters) and never the value,
* **clear override** removes the panel copy and the environment value applies again.

### Jev options

| provider | base URL | model | notes |
| --- | --- | --- | --- |
| `typesafe` (default) | `https://api.typesafe.ai` | `jev-latest` | official endpoint |
| `opencode` | `https://opencode.ai/inference` | `jev-1.13-free` | **free, limited-time** routing via the OpenCode Console key — it can be retired without notice; the client then stops with a clear error rather than silently switching to a paid model |
| `opencode-paid` | `https://opencode.ai/inference` | `jev-1.13` | paid OpenCode key |
| `opencode-zen` | `https://opencode.ai/zen` | `jev-1.13-free` | older Zen path, kept for compatibility |

Anything that is not the official host prints/records an **UNVERIFIED THIRD PARTY** warning:
whoever runs that gateway can log and reuse the state you send it. Leave `JEV_ALLOWED_HOSTS` empty to
accept the chosen provider's own host; set it explicitly to lock egress down harder.

## 4. Keeping it running on the free tier

* **Sleep.** A free Render web service sleeps after ~15 minutes without traffic; the first request
  afterwards takes a few seconds. The panel is a normal web app, so opening it wakes the service.
* **Scheduling.** Do not rely on platform cron for long work. Use the signed webhook with any free
  external scheduler (cron-job.org, GitHub Actions, UptimeRobot):

  ```bash
  body='{"action":"ping"}'
  sig=$(printf '%s' "$body" | openssl dgst -sha256 -hmac "$WORKER_WEBHOOK_SECRET" -hex | awk '{print $2}')
  curl -sS -X POST "https://<service>.onrender.com/webhook" \
       -H "content-type: application/json" -H "x-mishkah-signature: $sig" -d "$body"
  ```

  Actions: `ping` (audit + 200), `export` (invalidates the bundle cache so the next panel read is
  rebuilt). Anything else returns `501` and says so — long work belongs to the worker, not a web
  request.
* **Long jobs** (Telegram backfill, price backfill, replay, report) can be run from the Render
  **Shell** tab on the same service:

  ```bash
  python -m worker db status
  python -m worker channels sync
  python -m worker prices backfill --months 6
  python -m worker extract run --limit 500
  python -m worker replay range --from 2026-04-01 --to 2026-07-29
  python -m worker backtest run --split discovery
  python -m worker report build --split discovery
  ```

  The shell is the same container, so the panel and the shell always see the same database.

## 5. Local parity

```bash
.venv/bin/python -m worker db up              # migrations against the same database
.venv/bin/python -m worker server run         # http://0.0.0.0:8787 — the image's entry point
cd ui && npm run dev                          # UI on :3000, proxying /api to the worker on :8787
```

`ui/next.config.mjs` proxies `/api/*` to `MISHKAH_WORKER_URL` (default `http://127.0.0.1:8787`) in
development only. In the container the worker serves the exported UI itself, so the browser's
relative `/api/...` calls never leave the origin.

## 6. Troubleshooting

| symptom | cause / fix |
| --- | --- |
| `/healthz` returns 503 | database unreachable — check `DATABASE_URL` (pooled string, `sslmode=require`) |
| health is green but the panel says *connecting to the worker…* forever | the service woke up and the browser held a stale connection; reload |
| *Too many attempts* on login | 8 failed logins in 15 minutes, per window; wait or restart the service |
| a stored secret stops working after a redeploy | `MISHKAH_MASTER_KEY` changed — re-enter the values from the panel |
| Jev test says *host refused before sending anything* | the resolved host is not in `JEV_ALLOWED_HOSTS` |
| Jev answers say *mock* | `GM_DEMO_MODE=1`, or no API key is stored |
| panel shows *bundle is empty* | no data yet: run the demo seed or the real backfills from the Shell tab |

## 7. What never happens here

* No order-placing code path exists in the repository; `PAPER_ONLY=false` is refused loudly.
* The browser never holds an API key: secrets are written once, encrypted, and only ever shown as a
  fingerprint. Every panel write goes through the worker and lands in `audit_log`.
* Read-only Telegram: the worker has no send/post/forward code, and a unit test greps the source to
  keep it that way.
