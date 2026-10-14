# Mishkah · Mission Control (UI)

Single-page monitor for the research pipeline: live tiles, message timeline, channel leaderboard,
the Jev decision panel with a replay slider, the backtest/calibration view, coverage, the hypothesis
registry and the admin settings surface.

**This app renders; it does not compute.** Every number shown was produced by the Python worker.
No statistics, no gate verdicts and no state reconstruction happen in the browser or in a route
handler — that is the whole point of the architecture (rule R1: code does ALL statistics).

## Run it

**The panel reads everything from the worker API**, gated by the panel password
(`/api/bundle/*`), so there is no bundle file to keep in sync and no second source of truth.

```bash
# 1. the worker (this is where the data and the settings live)
.venv/bin/python -m worker server run          # http://0.0.0.0:8787

# 2. the UI in development (proxies /api/* to the worker, per next.config.mjs)
cd ui && npm install && npm run dev            # http://localhost:3000  (binds 0.0.0.0)

# 3. the production shape: a static export that the WORKER serves at / (see Dockerfile)
npm run build                                  # -> ui/out, served by `worker server run`
```

`npm run typecheck` runs `tsc --noEmit`. `npm start` only serves `out/` over plain HTTP — useful for
looking at layout, useless for data, because the API must be on the same origin.

`ui export` still exists and writes the same members to `ui/public/data` (git-ignored) for offline
inspection of the bundle or a Vercel-style reader deployment.

## Data contract (`/api/bundle/<member>`, served by the worker)

| file | contents |
|---|---|
| `meta.json` | generated_at, demo banner, split + window, session clock, tracked series, method version |
| `live.json` | one tile per tracked asset (price, source, resolution, age, `ok`/`stale`/`missing`), open paper position, last paper equity |
| `leaderboard.json` | latest `channel_scores` per channel: label, lead/lag minutes, net lifts, 95% CIs, q-values, pumper flag, measurement series |
| `decisions.json` | newest N `jev_decisions` for the split: state snapshot, question panel, answers with distributions, **the stored gate verdict**, state_hash |
| `timeline_index.json` | per day: message/signal counts, truncation flag, session-ready `price_path` |
| `timeline/<YYYY-MM-DD>.json` | that day's messages with `t` = minutes since the 08:00 Tehran open, raw Persian text, extraction, version flags |
| `backtest.json` | the artifact written by `backtest run`: equity, drawdown, baselines, calibration, summary |
| `coverage.json` | per-series per-month bar coverage + the M1 acceptance verdicts |
| `hypotheses.json` | the hypothesis registry and each coded verdict |
| `run_config.json` | runtime settings (no secrets) + the frozen `run_config` rows |

Every member is built on demand from the database (30 s in-process cache, `MISHKAH_BUNDLE_TTL`);
`python -m worker ui export` writes the identical members to `ui/public/data` for offline use. The
browser never writes data: panel writes go to `/api/admin/*`, which the worker validates, applies and
audits (it is the only process that touches Postgres).

## Anti-leakage guarantees the UI leans on

- `decisions.json` rows come from `jev_decisions`, i.e. from states built by
  `worker.research.state` through `ReadContext` (`published_at <= T`, versioned edits). The
  replay slider is therefore not a separate code path that could show future messages.
- `timeline/<day>.json` is exported with an `as_of` of that day's 22:00 Tehran close, so an edit
  ingested today does not retroactively appear in a past day's timeline.
- Marker minute offsets are computed in Python with `zoneinfo` (Iran has DST); the browser never
  does timezone arithmetic.

## Deployment notes (Render — see `docs/deploy_render.md`)

`next.config.mjs` sets `output: "export"`: the app is a static bundle with no server at runtime, and
the **worker serves it** (`ui/out`) on the same origin as `/api/*`. That is what makes the panel
work on a single free Render web service:

- no database driver, no `server-only` reads, no CORS, no proxy hop;
- the password gate is enforced by the worker (`/api/auth/*` + session cookie) — the UI only decides
  what to show;
- no cron and no long-running function: scheduling is the signed `/webhook`, and the worker does the
  long work.
- `MISHKAH_WORKER_URL` only matters in development (where `next dev` proxies `/api/*` to it).

## Files

```
app/page.tsx              client page: gate → bundle fetch + polling → shell
components/AuthGate.tsx   first-run password setup / login (worker-issued session cookie)
components/AdminView.tsx  settings groups, per-group tests, run_config writes, session controls
components/MissionControl.tsx  shell: rail + header + banner + view switch + provenance footer
components/Rail.tsx       the left icon rail (8 views)
components/LiveView.tsx|TimelineView|LeaderboardView|DecisionView|BacktestView|CoverageView|HypothesesView|AdminView.tsx
components/Bits.tsx       Panel/Chip/Stat/CiBar/Ring/StackedBar/LineChart/KeyValue primitives
lib/types.ts              bundle types — keep in sync with worker/worker/cli/ui_cmd.py
lib/api.ts                browser → worker API (auth, settings, bundle) — no secrets, no math
lib/data.ts               bundle loaders (client-side, same origin, session cookie included)
lib/format.ts             every number/label/digit conversion in the app
```
