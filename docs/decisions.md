# Decisions log

Per the work protocol (§13), anything that is not dictated by the build prompt is decided here,
documented once, and then treated as a fixed convention until this file says otherwise. Entries
are append-only: if a decision is reversed, the old entry stays and the reversal is added below it.

Format: **D# · date · decision — why — consequences.**

---

## D1 · anti-leakage read bound
**Decision.** Every historical read goes through `worker.research.asof.ReadContext`, which queries
the `messages_asof(:as_of)` view. A message version `v1` is visible from `published_at`; any later
version (`v>1`, i.e. an edit or a forward) additionally requires `ingested_at <= as_of`; among the
visible versions the newest one wins.
**Why.** The prompt fixes the bound as `published_at <= T`. Reconstructing the *editable* history
weakens that bound, so the strictest reading is used: edits can never be seen before we knew them.
**Consequences.** `assert_in_split` guards every read (T5). Tests T1–T6 in `worker/tests/` fail if
the bound ever loosens. Ingestion timestamps never overwrite publish timestamps.

## D2 · split boundaries are half-open
**Decision.** `Split.days("discovery")` = `[start_date, discovery_end)`; `Split.days("outofsample")`
= `[discovery_end, outofsample_end)`. The boundary lives in ONE place (`worker/window.py`).
**Why.** The first discovery backtest attempt tried to replay `discovery_end` itself
(2026-08-01) and `ReadContext` refused it — correct behaviour from the guard, wrong day list from
the caller. Two independent day-range implementations is exactly how out-of-sample windows get
leaked into.
**Consequences.** `backtest.split_days()` delegates to `Split.days()`; callers must never rebuild
day ranges themselves. `run_config['split']` is written once and frozen.

## D3 · cost convention: gross is mid-to-mid, friction is charged ONCE
**Decision.** For a paper round trip: `gross_bps = (exit_level / entry_mid − 1) × 1e4` where
`exit_level` is a **mid** level; `cost_bps = spread + 2 × slippage` (round trip);
`net_bps = gross_bps − cost_bps`. `entry_price`/`exit_price` remain the ask/bid **fill** prices for
the ledger and for `pnl_toman`, and `detail_json` carries `exit_level`, `gross_basis` and
`cost_basis`.
**Why.** This was a real bug, caught by the discovery backtest. The original code computed gross
from the ask/bid fill prices — which already contain the spread — and then subtracted the cost
again, so every flat trade netted exactly −119.82 bps instead of −60 bps (row 2026-04-04:
`entry_mid = exit_mid = 9,596,000`, gross −59.82 = −round trip, net −119.82). The golden fixture
`tests/fixtures/costs_golden.json` *encoded* the bug (it derived expected gross from the fill
prices), so the fixture was rewritten under the new convention, not "made to pass".
**Consequences.** Signal-level lifts keep the **one-sided** cost (`spread + slippage`, D3a in
`research/costs.py`); a round trip uses `spread + 2×slippage`. Any future change to this convention
must update the fixture and this entry together.

## D4 · stop / day-cap binding rule
**Decision.** Both thresholds are derived from the ask-based entry price:
`stop_threshold = entry × (1 − stop_loss_bps/1e4)` and
`cap_threshold = entry × (1 − (cap_room_bps + round_trip_cost_bps)/1e4)` with
`cap_room_bps = max(0, |daily_loss_cap_bps| − max(0, −day_pnl_bps))`. Because `day_pnl_bps` is
**negative** on a down day, the remaining budget is the cap minus the realised loss. On the way down
the **higher** level is touched first and therefore binds (`stop` if stop ≥ cap, else `cap`).
**Why.** Two wrong versions were tried: keeping the cap below the stop always, and
`abs(cap) − day_pnl_bps` (which *adds* the loss to the budget).
**Consequences.** `detail_json` records `binding_threshold`, `cap_room_bps`, `session_low/high` and
`day_pnl_bps_before`, so any exit can be re-derived by hand.

## D5 · lead/lag sign convention
**Decision.** `lag = signal minute − return minute`. `lag ≤ 0` ⇒ the channel **LEADS** the market;
`lag > 0` ⇒ it **LAGS**. `LAG_GRID` = −60…+60 in 5-minute steps; a label needs ≥20 events.
**Why.** The first implementation paired intensity and returns in the opposite direction and
labelled every channel backwards (demo publishers came out as laggers).
**Consequences.** This convention is the contract between `leadlag.py`, `scoring.py` and every
report table. Do not flip it.

## D6 · one FDR family, heterogeneous keys, CIs always
**Decision.** All discovery-time tests land in the pre-registered family
`phase1-preregistered` (BH, q = 0.10) via `stats/fdr.py`. The family covers event studies, lead/lag
peaks and agreement levels, so a ~4-month run is ~380 tests. Every result is reported with a
day-level block-bootstrap 95% CI; a p-value is never shown alone.
**Why.** Two failure modes: the prompt's "correct across the FULL tested family" rule, and the fact
that `bh_correct()` rows carry **heterogeneous key shapes**
(`("event", user, kind, direction, horizon)`, `("leadlag", user, "lagN")`, `("agreement", kN, horizon)`).
A consumer that unpacks keys naively crashed and, worse, once mis-assigned q-values silently.
**Consequences.** Every consumer filters keys by length/prefix. Anything explored after seeing the
data is labelled "exploratory, uncorrected" in the report.

## D7 · the single strong LLM is a skippable explainer
**Decision.** `research/explainer.py` is the only strong-LLM call in the system. It receives the
numeric artifact (not raw data), writes the report's narrative, and proposes hypotheses that are
inserted into `hypotheses` with `status='proposed'`. It is called only by `report build`, and only
when both `EXPLAINER_MODEL` and `OPENROUTER_API_KEY` are set; otherwise the step is skipped and the
report says so. `research/hypothesis.py` holds the coded test registry: six coded tests with a fixed
CI-based verdict rule; a hypothesis matching no coded test stays `proposed` with
`test_method='unmapped'`.
**Why.** R1: cheap models extract, Jev answers a fixed question tree, code does ALL statistics, and
the strong model only writes narrative and proposes hypotheses. Making the narrative optional keeps
the pipeline reproducible without any API key.
**Consequences.** The narrative can never gate, filter or re-rank anything — no caller reads it
except the report writer. Hypotheses only become `confirmed`/`rejected` through code.

## D8 · demo mode is labelled everywhere, and degenerate marks are reported
**Decision.** `GM_DEMO_MODE=1` makes ingestion/market access read `worker/demo` instead of the
network. Every report and every CLI run under it carries a synthetic-data banner; `backtest_runs`
records `mock: true` in `config_json`. Trades store the provenance of both marks
(`entry_mark`, `exit_mark`, `same_bar_mark`) and the report prints a mark-basis table.
**Why.** In demo the only domestic-gold series is a **daily** bar (`synthetic`, 1d, stamped 08:30Z =
12:00 Tehran). A checkpoint at or after that timestamp reads the same bar for entry and for the
22:00 exit, so the mid move is exactly 0 and the row measures execution cost rather than price
movement. The old report showed a smooth −157 bps/day curve that looked like a strategy result.
**Consequences.** The report now states "42 of 42 trades entered and exited on the SAME bar" and
tells the reader to treat the equity curve as a cost study. A real intraday marking series (the live
minute logger, M9) is what removes the caveat; the demo reseed after the channel-model patch is
recorded in `docs/source_notes.md`.

## D9 · missing data is shown, never filled
**Decision.** `prices/coverage.py` writes gap rows and per-series per-month coverage percentages;
`prices verify` exits non-zero and writes `docs/coverage_report.md` when the M1 thresholds fail
(USDTIRT 1m below 80% of trading days, XAUUSD hourly below 90%). Rows are never interpolated,
never forward-filled, and never removed from the coverage numerator.
**Why.** The prompt forbids inventing data; silently "fixing" coverage is the failure mode that
makes a 6-month study look better than it is.
**Consequences.** A source that behaves differently than documented is a STOP-and-record event
(`docs/source_notes.md` with probe output), never a data patch.

## D10 · stop trigger geometry is deliberately conservative
**Decision.** `stop_threshold` and `cap_threshold` are derived from the **ask** entry price while
`session_low` comes from **mid** bars, so a stop is triggered a hair later than a mid-based stop
would be (≈ round trip's worth of slack).
**Why.** The trader's real exit is a sell, i.e. the bid side; comparing the day's mid low against
the ask-derived threshold keeps the trigger from firing on a spread-width wiggle. It is also the
geometry the golden fixture has encoded since M5.
**Consequences.** Documented as an approximation, not a claim of tick accuracy; slippage/spread are
flat per-asset bps rather than a function of volatility. Revisit only after M8, together with the
execution model as a whole.

## D11 · failed and aborted runs are recorded, never deleted
**Decision.** `backtest_runs` keeps one row per attempt with `status in (running, ok, failed)` and
the failure reason in `notes`. A `running` row left behind by a crash is closed as `failed` by
housekeeping (audit-logged), not removed.
**Why.** The prompt requires every external call to be cached/resumable and honesty about what
happened. Runs 4 and 5 of the discovery backtest failed on the split-boundary bug (D2) before run 6
succeeded; deleting them would hide that the boundary guard actually fired.
**Consequences.** "How many times did we touch the out-of-sample window" is answerable from the DB,
which is what makes the one-shot OOS rule enforceable.

## D12 · all timestamps UTC, Tehran only via `zoneinfo`
**Decision.** Every timestamp column is `timestamptz` storing UTC. Tehran-local boundaries (08:00
open, 22:00 forced flat, trading weekdays Sat–Wed) are computed with `zoneinfo.ZoneInfo` through the
helpers in `worker/timez.py`; nothing else computes offsets.
**Why.** Iran reinstated DST in 2022; a hardcoded `+03:30` would silently shift the whole session
clock for part of the year.
**Consequences.** `ZoneInfo.utcoffset(None)` is a trap (returns `None`) — use
`datetime.now(tz=TEHRAN).utcoffset()`.

## D13 · runtime placement: worker on an always-on host, UI served with no database driver
**Decision (operator asked me to choose).** Heavy work stays in the Python worker, and the worker
runs on a machine that is awake during the session (the operator's own machine is fine to start;
an always-on box is required before the live monitor is trusted, because a sleeping laptop turns
the minute logger into a swiss-cheese series and the UI will honestly paint those tiles amber).
Postgres lives next to the worker. The Next.js UI is deployable to Vercel as a **static/bundle
reader**: it has no database driver, no cron and no long-running function. It renders only the
bundle that `python -m worker ui export` writes, and its only write path is the token-gated
`/api/admin` route, which forwards to the worker (`WORKER_ADMIN_URL`) or answers 501 — never a
direct SQL write.
**Why.** Three constraints collide: the free-tier rule ("UI + short read-only API routes only, no
Vercel cron"), the fact that Vercel cannot reach a laptop-local Postgres, and rule R7's requirement
that the UI's live tiles be *fresh* during 08:00–22:00 Tehran. Making the UI a pure consumer of a
worker-written bundle satisfies all three and keeps one writer for the audit log.
**Consequences.** Nothing in the UI can silently disagree with a report — it is the same JSON the
report is built from. `live.json` staleness is therefore meaningful: amber means the worker has not
been running, which is exactly the signal the operator needs. If the worker later runs on a
different host than Postgres, the export step is the only thing that has to move with it.

## D14 · narrative stays off until a key exists
**Decision.** The explainer (the only strong-LLM call, D7) stays disabled in this deployment: no
`EXPLAINER_MODEL` / `OPENROUTER_API_KEY` is configured, so `report build` writes every numeric
section and prints "narrative skipped by design" in section 7. The UI has a hypotheses view that
will populate the moment the step is enabled.
**Why.** The operator chose "numbers only for now" (2026-10-13). The numeric pipeline must never
depend on an API key being present.
**Consequences.** No hypotheses are proposed automatically yet; `hypotheses` stays empty until the
operator either enables the explainer or inserts rows by hand (the coded tests in
`research/hypothesis.py` are what turn a proposal into a verdict, either way).

## D15 · deployment topology supersedes D13: ONE Render web service serves the panel and the API
**Decision (operator: "میبرم روی render.com و deploy میکنم").** The system ships as a single Docker
image (`Dockerfile`) described by `render.yaml`: uvicorn serves the **static Next.js export**
(`ui/out`) at `/`, the panel API at `/api/*`, the bundle at `/api/bundle/*` and the signed webhook at
`/webhook`. Postgres stays external (Neon/Supabase free tier). The UI has no database driver, no
server components that read the filesystem, and no `/data` folder of its own: the browser reads the
bundle from the worker API, which rebuilds every member from the database (30 s in-process cache,
invalidated by the webhook or any panel write).
**Why.** D13 assumed Vercel could read a bundle file that the worker wrote somewhere. On Render that
becomes a shared-filesystem problem (a container's disk is not the operator's disk, and a free
instance's disk is ephemeral), and it would also mean either two services or a data pipeline that can
silently go stale. Serving the API and the UI from one origin removes CORS, removes the proxy hop,
and makes "what the panel shows" and "what the worker computed" the same request.
**Consequences.** The `ui export` CLI stays — it is how the bundle is inspected offline and how the
report is cross-checked — but nothing in the deployment depends on it any more (`MISHKAH_BUNDLE_DIR`
points a container's export at `/tmp`, or a mounted disk). `WORKER_ADMIN_URL` and the token-gated
Next `/api/admin` route are deleted; a panel write goes to the worker directly. Free-tier sleep is
acceptable because the panel is a normal web page: opening it wakes the service, and the tiles
honestly go amber while it was asleep. Vercel remains possible as a *reader* (the export is a static
site) but is no longer the documented path; "no reliance on platform cron" still holds — scheduling
is the signed webhook (D16).

## D16 · panel access: one operator-set password → scrypt hash → session cookie
**Decision.** The Mission Control is gated by a password the operator sets on first visit
(`/api/auth/setup`, refused once a password exists anywhere) or bootstraps from `UI_ADMIN_PASSWORD`
in the environment. The password is hashed with scrypt (`scrypt$N$r$p$salt$hash`) and stored in
`app_secrets` under the name `UI_ADMIN_PASSWORD` — never in plaintext, never returned by any endpoint.
Login issues an opaque 32-byte token; only its sha256 is stored (`ui_sessions`), and it travels in an
HttpOnly, SameSite=Lax cookie (HTTP-only, so a `secure` flag is added automatically when the request
is HTTPS). Eight failed logins in 15 minutes block further attempts per window, and every attempt is
recorded. **Bundle reads require the session too** — the panel is the door, not a decoration.
**Why.** The operator asked for it in so many words ("یکجایی بزاری که یک پسورد بزارم و وارد بشم"):
the panel will be on the public internet and it is where API keys get typed. A single password with
throttling is the honest amount of security for a one-operator research panel; per-user accounts
would be pretending to an access-control story that does not exist.
**Consequences.** Losing the password means a redeploy with `UI_ADMIN_PASSWORD` set (or
`MISHKAH_MASTER_KEY` preserved and `app_secrets` edited) — the master key is what decrypts; the
password only gates. Logging out on a shared machine still matters: changing the password does not
revoke existing sessions (they expire on their own schedule, `UI_SESSION_MINUTES`, default 12 h).

## D17 · settings precedence: panel > environment > package default; secrets write-only
**Decision.** `settings_store.py` defines five groups / 21 fields (Jev, extraction+narrative,
Telegram, price sources, panel). Resolution per field: a panel value if one is stored, else the
process environment, else the package default — and clearing a panel value **falls back** instead of
vanishing. Values are validated and coerced before they are stored (selects, booleans, numbers) so a
typo is rejected in the panel rather than surfacing as a `ConfigError` inside a night run. Secrets are
stored AES-256-GCM encrypted under `MISHKAH_MASTER_KEY` (env, or a local `.secrets/master.key`
created 0600 on first use); the API returns a fingerprint (`sha256:…` + last four characters) and
never the value; the browser can therefore never hold an API key, and a database dump alone reveals
nothing usable.
**Why.** The operator asked for panel-managed keys that are "easy and precise" without exposing
secrets to the browser ("بتونم بقیه api ها رو هم ست کنم راحت و حرفهای و دقیق"), and R2 requires the Jev
host allowlist to be enforced *before* anything leaves the process — which a stored `JEV_ALLOWED_HOSTS`
must therefore obey too.
**Consequences.** A panel value takes effect on the worker's next call (long-lived processes re-read
`effective_settings(conn)`; the API does it per request), so no redeploy is needed to rotate a key.
An unknown provider (panel or env) is a loud `ConfigError`, never a silent fallback. `MISHKAH_MASTER_KEY`
must be backed up: it is deliberately not stored in the database, so stored keys are unrecoverable
without it.

## D18 · how the local UI gets its data: `/api/bundle/*`, same origin, session-gated
**Decision.** The exported UI fetches every bundle member from `/api/bundle/<member>` (timeline days
included) with `credentials: "include"`. `ui/public/data` remains a local artifact of `ui export`
and is only used by `next dev` (whose config proxies `/api/*` to the worker on :8787 in development).
**Why.** A static export cannot read the filesystem at request time; a build-time snapshot would go
stale the moment the worker writes a row, and a second copy of the bundle is a second truth. One
origin keeps the session cookie usable and the panel's gate meaningful.
**Consequences.** If the browser is open while the worker sleeps (free tier), reads fail and the UI
keeps the last good bundle on screen with its (now old) timestamp rather than inventing freshness.
`MISHKAH_BUNDLE_TTL` controls the in-process cache; the webhook's `export` action invalidates it.
