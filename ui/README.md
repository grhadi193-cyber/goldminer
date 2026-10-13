# Mishkah · Mission Control (UI)

Single-page monitor for the research pipeline: live tiles, message timeline, channel leaderboard,
the Jev decision panel with a replay slider, the backtest/calibration view, coverage, the hypothesis
registry and the admin settings surface.

**This app renders; it does not compute.** Every number shown was produced by the Python worker.
No statistics, no gate verdicts and no state reconstruction happen in the browser or in a route
handler — that is the whole point of the architecture (rule R1: code does ALL statistics).

## Run it

```bash
# from the repository root — generate the bundle the UI reads
.venv/bin/python -m worker ui export --split discovery --days 45 --clean

cd ui
npm install
npm run dev            # http://localhost:3000  (binds 0.0.0.0)
```

`npm run build && npm start` for a production build, `npm run typecheck` for `tsc --noEmit`.

## Data contract (`public/data`, generated, git-ignored)

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

Regeneration is idempotent: `ui export` rewrites the directory from the database, and `--clean`
wipes it first. Nothing in the UI writes back except the token-gated admin route.

## Anti-leakage guarantees the UI leans on

- `decisions.json` rows come from `jev_decisions`, i.e. from states built by
  `worker.research.state` through `ReadContext` (`published_at <= T`, versioned edits). The
  replay slider is therefore not a separate code path that could show future messages.
- `timeline/<day>.json` is exported with an `as_of` of that day's 22:00 Tehran close, so an edit
  ingested today does not retroactively appear in a past day's timeline.
- Marker minute offsets are computed in Python with `zoneinfo` (Iran has DST); the browser never
  does timezone arithmetic.

## Deployment notes (Vercel)

The app is a static-renderable Next 14 App Router project: pages read the bundle with `fs`, routes
are `force-dynamic` where a fresh read matters. On Vercel:

- put the bundle in the deployment (or mount it from object storage) — the UI has no database
  driver and never will: `WORKER_ADMIN_URL` is the only bridge back to the worker.
- `UI_ADMIN_TOKEN` (env, server-side) gates `/api/admin`. Without it the route answers 503; with it
  but without `WORKER_ADMIN_URL` it answers 501 and tells the operator to run the change on the
  worker host, where the audit log lives.
- no cron, no long-running function: the worker does the long work and writes the bundle.

## Files

```
app/page.tsx              server component: load the bundle, hand it to the shell
app/api/admin/route.ts    token-gated change requests (never writes to Postgres itself)
components/MissionControl.tsx  shell: rail + header + banner + view switch + provenance footer
components/Rail.tsx       the left icon rail (8 views)
components/LiveView.tsx|TimelineView|LeaderboardView|DecisionView|BacktestView|CoverageView|HypothesesView|AdminView.tsx
components/Bits.tsx       Panel/Chip/Stat/CiBar/Ring/StackedBar/LineChart/KeyValue primitives
lib/types.ts              bundle types — keep in sync with worker/worker/cli/ui_cmd.py
lib/data.ts               server-only bundle loaders
lib/format.ts             every number/label/digit conversion in the app
```
