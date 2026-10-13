# Source notes — what each API actually did

Rule (§13): when a source behaves differently than its documentation (or than our code assumes),
**STOP**, record the difference here with the raw probe output, then adapt the ingestion module.
Never patch data to fit an assumption, never silently skip a coverage failure.

Status key: `unprobed` (no real call made yet) · `probed-ok` · `probed-differs` · `blocked`.

| source | module | status | note |
|---|---|---|---|
| Nobitex market/udf (`USDTIRT`) | `worker/worker/prices/nobitex.py` | **unprobed** | The module docstring cited a probe and a `docs/source_notes.md` entry that do not exist yet. M1 (`scripts/verify_sources.py` / `prices verify`) is the first real contact; its raw output gets pasted below. |
| yfinance `XAUUSD` | `worker/worker/prices/xauusd.py` | **unprobed** | Hourly bars; M1 acceptance is 90% of trading days. yfinance is not installed in the venv yet (network/proxy question) — install and probe at M1. |
| TGJU scraper | `worker/worker/prices/tgju.py` | **unprobed** | `gold_18k_toman_per_gram`, `emami_coin_toman`, `usd_free_market_toman`. Unofficial page; watch for rate limits and for the page's "last price" being indicative rather than tradeable. |
| TSETMC | `worker/worker/prices/tsetmc.py` | **unprobed** | ~0.1 g tradable gold-fund proxy; symbol discovery is a real step, not an assumption. |
| Telegram (Telethon) | `worker/worker/telegram/client.py` | **unprobed** | Read-only by design (T10 greps the source for send/post/forward usage). Requires `TELEGRAM_API_ID/HASH/SESSION`; telethon is not installed yet. Backfill is resumable through `ingest_progress:telegram:{username}`. |
| Jev (typesafe.ai) | `worker/worker/research/jev.py` | **unprobed (real)** | Only ever called with the key from `JEV_BASE_URL`/`JEV_API_KEY`; host must be in `JEV_ALLOWED_HOSTS`. `scripts/jev_smoke_test.py` must pass before any real call is trusted (operator checkpoint b). Everything run so far used the mock transport. |

## Probe log

_Empty. Each entry will be appended as `date · command · raw output (trimmed) · conclusion`._
