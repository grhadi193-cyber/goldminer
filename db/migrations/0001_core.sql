-- 0001_core: core schema for Mishkah (Phase 1 research/paper).
-- Conventions (R3, R10):
--   * every timestamp is timestamptz stored in UTC
--   * event time (`published_at`, `ts`, `decision_ts`) is never overwritten by ingestion time
--   * `ingested_at` / `computed_at` exist for audit and for the edit-visibility rule

-- migrate:up

create table if not exists channels (
    id            bigserial primary key,
    username      text        not null unique,
    title         text,
    added_at      timestamptz not null default now(),
    active        boolean     not null default true,
    notes         text,
    -- operator hint used only by the UI leaderboard grouping; never used as a signal
    tier          text        not null default 'unclassified'
);

create table if not exists messages (
    id              bigserial   primary key,
    channel_id      bigint      not null references channels(id) on delete cascade,
    telegram_msg_id bigint      not null,
    published_at    timestamptz not null,          -- Telegram's own message date (UTC)
    ingested_at     timestamptz not null default now(),
    edited_at       timestamptz,                   -- when Telegram reported the edit (UTC)
    raw_text        text,
    version         integer     not null default 1,
    is_edited       boolean     not null default false,
    media_ref       text,
    constraint messages_channel_msg_version_uniq unique (channel_id, telegram_msg_id, version)
);

create index if not exists messages_published_at_idx on messages (published_at desc, channel_id);
create index if not exists messages_channel_published_idx on messages (channel_id, published_at desc);

create table if not exists extractions (
    message_id      bigint      primary key references messages(id) on delete cascade,
    model           text        not null,
    extraction_json jsonb,
    status          text        not null check (status in ('ok', 'failed')),
    status_reason   text,                          -- e.g. extraction_failed, schema_invalid, empty_text
    attempts        integer     not null default 0,
    cache_key       text        not null unique,   -- sha256(raw_text + '|' + model)
    extracted_at    timestamptz not null default now(),
    computed_at     timestamptz not null default now()
);

create index if not exists extractions_status_idx on extractions (status);

create table if not exists price_bars (
    source      text        not null,   -- nobitex|tgju|tsetmc|yfinance|live_logger|synthetic
    symbol      text        not null,
    ts          timestamptz not null,
    resolution  text        not null check (resolution in ('1m', '5m', '1h', '1d')),
    open        double precision,
    high        double precision,
    low         double precision,
    close       double precision not null,
    volume      double precision,
    ingested_at timestamptz not null default now(),
    constraint price_bars_uniq unique (source, symbol, resolution, ts)
);

create index if not exists price_bars_lookup_idx on price_bars (symbol, resolution, ts);
create index if not exists price_bars_source_ts_idx on price_bars (source, symbol, ts);

create table if not exists price_gaps (
    id          bigserial primary key,
    source      text        not null,
    symbol      text        not null,
    from_ts     timestamptz not null,
    to_ts       timestamptz not null,
    reason      text        not null,
    detected_at timestamptz not null default now(),
    constraint price_gaps_uniq unique (source, symbol, from_ts, to_ts, reason)
);

create table if not exists channel_scores (
    id                bigserial primary key,
    channel_id        bigint      not null references channels(id) on delete cascade,
    asof_date         date        not null,
    horizon           text        not null default 'eod',   -- 30m|1h|3h|eod
    lead_lag_minutes  integer,
    lift_bps_30m      double precision,
    lift_bps_1h       double precision,
    lift_bps_3h       double precision,
    lift_bps_eod      double precision,
    net_of_costs      boolean     not null default true,
    n_events          integer     not null default 0,
    ci_low_bps        double precision,
    ci_high_bps       double precision,
    fdr_corrected     boolean     not null default false,
    q_value           double precision,
    label             text        not null check (label in ('lead', 'lag', 'noise', 'insufficient')),
    pumper_flag       boolean     not null default false,
    quoted_bias_pct   double precision,
    method_version    text        not null,
    extra_json        jsonb,
    computed_at       timestamptz not null default now(),
    constraint channel_scores_uniq unique (channel_id, asof_date, horizon, method_version)
);

create index if not exists channel_scores_lookup_idx on channel_scores (asof_date desc, horizon);

create table if not exists jev_cache (
    cache_key     text        primary key,          -- sha256 of canonical request
    request_json  jsonb       not null,
    response_json jsonb       not null,
    model         text,
    created_at    timestamptz not null default now()
);

create table if not exists jev_decisions (
    decision_id    bigserial   primary key,
    decision_ts    timestamptz not null,
    day_date       date        not null,
    checkpoint     text        not null default '0800',
    state_json     jsonb       not null,
    questions_json jsonb       not null,
    answers_json   jsonb       not null,
    model          text,
    cache_key      text,
    split          text        not null check (split in ('discovery', 'outofsample', 'live', 'demo')),
    state_hash     text        not null,
    computed_at    timestamptz not null default now(),
    constraint jev_decisions_uniq unique (day_date, checkpoint, split, state_hash)
);

create index if not exists jev_decisions_day_idx on jev_decisions (day_date desc, checkpoint);

create table if not exists backtest_runs (
    run_id      bigserial   primary key,
    config_json jsonb       not null,
    split       text        not null,
    status      text        not null default 'running' check (status in ('running', 'ok', 'failed')),
    started_at  timestamptz not null default now(),
    finished_at timestamptz,
    notes       text
);

create table if not exists backtest_trades (
    id          bigserial primary key,
    run_id      bigint      not null references backtest_runs(run_id) on delete cascade,
    decision_id bigint      references jev_decisions(decision_id) on delete set null,
    day_date    date        not null,
    direction   text        not null check (direction in ('long', 'flat')),
    entry_ts    timestamptz not null,
    exit_ts     timestamptz not null,
    entry_price double precision not null,
    exit_price  double precision not null,
    size_g      double precision not null,
    gross_bps   double precision not null,
    cost_bps    double precision not null,
    net_bps     double precision not null,
    exit_reason text        not null check (exit_reason in ('signal', 'stop', 'flat_2200', 'cap')),
    pnl_toman   double precision,
    detail_json jsonb
);

create index if not exists backtest_trades_run_idx on backtest_trades (run_id, day_date);

create table if not exists day_outcomes (
    day_date       date primary key,
    decision_price double precision not null,
    eod_price      double precision not null,
    realized_bps   double precision not null,
    label          text not null check (label in ('up', 'down', 'flat')),
    series         text not null default 'gold_18k_toman_per_gram',
    computed_at    timestamptz not null default now()
);

create table if not exists paper_positions (
    id           bigserial primary key,
    day_date     date        not null,
    opened_ts    timestamptz not null,
    closed_ts    timestamptz,
    direction    text        not null check (direction in ('long', 'flat')),
    size_g       double precision not null,
    entry_price  double precision not null,
    exit_price   double precision,
    gross_bps    double precision,
    cost_bps     double precision,
    net_bps      double precision,
    exit_reason  text,
    decision_id  bigint references jev_decisions(decision_id) on delete set null,
    mode         text not null default 'paper' check (mode = 'paper'),
    created_at   timestamptz not null default now()
);

create index if not exists paper_positions_day_idx on paper_positions (day_date desc);

create table if not exists paper_orders (
    id          bigserial primary key,
    position_id bigint references paper_positions(id) on delete cascade,
    ts          timestamptz not null,
    side        text not null check (side in ('buy', 'sell')),
    qty_g       double precision not null,
    price       double precision not null,
    reason      text,
    mode        text not null default 'paper' check (mode = 'paper'),
    created_at  timestamptz not null default now()
);

create table if not exists paper_equity (
    ts          timestamptz primary key,
    equity_toman double precision not null,
    day_date    date not null,
    mode        text not null default 'paper' check (mode = 'paper'),
    created_at  timestamptz not null default now()
);

create table if not exists hypotheses (
    id               bigserial primary key,
    text             text not null,
    proposed_by_model text,
    proposed_at      timestamptz not null default now(),
    status           text not null default 'proposed'
        check (status in ('proposed', 'tested', 'rejected', 'confirmed')),
    test_method      text,
    test_results_json jsonb,
    tested_at        timestamptz,
    run_id           bigint references backtest_runs(run_id) on delete set null,
    unique (text, proposed_by_model)
);

create table if not exists run_config (
    key      text primary key,
    value_json jsonb not null,
    set_at   timestamptz not null default now(),
    set_by   text not null default 'system'
);

create table if not exists audit_log (
    id          bigserial primary key,
    ts          timestamptz not null default now(),
    actor       text not null,
    event       text not null,
    detail_json jsonb
);

create index if not exists audit_log_ts_idx on audit_log (ts desc);

-- migrate:down

drop table if exists audit_log cascade;
drop table if exists run_config cascade;
drop table if exists hypotheses cascade;
drop table if exists paper_equity cascade;
drop table if exists paper_orders cascade;
drop table if exists paper_positions cascade;
drop table if exists day_outcomes cascade;
drop table if exists backtest_trades cascade;
drop table if exists backtest_runs cascade;
drop table if exists jev_decisions cascade;
drop table if exists jev_cache cascade;
drop table if exists channel_scores cascade;
drop table if exists price_gaps cascade;
drop table if exists price_bars cascade;
drop table if exists extractions cascade;
drop table if exists messages cascade;
drop table if exists channels cascade;
