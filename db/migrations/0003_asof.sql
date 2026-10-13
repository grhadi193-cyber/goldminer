-- 0003_asof: the anti-leakage visibility rule lives in ONE place, in SQL, so both the
-- worker and any read-only consumer agree on what "visible at time T" means (R3).
--
-- Visibility rule:
--   * the original version (version = 1) of a message becomes visible at its
--     `published_at` (Telegram's own message date, UTC) — regardless of when our
--     backfill actually ingested it.
--   * an edited version (version > 1) keeps the ORIGINAL published_at, but only becomes
--     visible once the edit was actually ingested (`ingested_at <= as_of`).
--   * among visible versions, the highest version wins.

-- migrate:up

create or replace function messages_asof(as_of timestamptz)
returns table (
    id              bigint,
    channel_id      bigint,
    telegram_msg_id bigint,
    published_at    timestamptz,
    ingested_at     timestamptz,
    raw_text        text,
    version         integer,
    is_edited       boolean,
    media_ref       text
)
language sql
stable
as $$
    select distinct on (m.channel_id, m.telegram_msg_id)
           m.id, m.channel_id, m.telegram_msg_id, m.published_at, m.ingested_at,
           m.raw_text, m.version, m.is_edited, m.media_ref
    from messages m
    where m.published_at <= as_of
      and (m.version = 1 or m.ingested_at <= as_of)
    order by m.channel_id, m.telegram_msg_id, m.version desc
$$;

comment on function messages_asof(timestamptz) is
    'Latest message version visible at as_of under R3 (no look-ahead). Single source of truth.';

-- migrate:down

drop function if exists messages_asof(timestamptz);
