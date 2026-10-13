-- 0006: admin panel state — encrypted secrets, login sessions, login attempts.

-- migrate:up
-- Panel-managed configuration. Values are AES-256-GCM encrypted with MISHKAH_MASTER_KEY and are
-- NEVER returned by any API: the panel only ever sees a fingerprint. Env vars still work and win
-- at process start; a value stored here is applied on top of the environment by the worker.
create table if not exists app_secrets (
    name        text primary key,
    ciphertext  text,
    fingerprint text,
    updated_at  timestamptz not null default now(),
    updated_by  text not null default 'panel'
);

-- Sessions are stored hashed: leaking this table does not hand out live sessions.
create table if not exists ui_sessions (
    token_hash text primary key,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    label      text
);

create table if not exists login_attempts (
    id    bigserial primary key,
    ts    timestamptz not null default now(),
    ok    boolean not null,
    label text,
    note  text
);

create index if not exists login_attempts_ts_idx on login_attempts (ts desc);

-- migrate:down
drop index if exists login_attempts_ts_idx;
drop table if exists login_attempts;
drop table if exists ui_sessions;
drop table if exists app_secrets;
