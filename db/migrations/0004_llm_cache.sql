-- 0004_llm_cache: separate LLM cache table + relax a constraint that would lose data.
--
-- Why: `extractions.cache_key` was UNIQUE(raw_text+model). Identical channel texts really do
-- repeat (bots repost, channels copy each other), and with a cross-message UNIQUE constraint
-- the second message would silently lose its extraction row — which breaks the invariant
-- "every message has a status". The *cache identity* stays exactly what the spec asks for
-- (sha256 of raw_text+model) but it now lives on its own table, where uniqueness is the point
-- (it is what prevents paying for the same translation twice). See docs/decisions.md #4.

-- migrate:up

create table if not exists llm_cache (
    cache_key     text primary key,      -- sha256(raw_text + '|' + model)
    model         text not null,
    status        text not null check (status in ('ok', 'failed')),
    response_json jsonb,
    raw_response  text,
    reason        text,
    hits          integer not null default 0,
    created_at    timestamptz not null default now()
);

alter table extractions drop constraint if exists extractions_cache_key_key;
create index if not exists extractions_cache_key_idx on extractions (cache_key);

-- migrate:down

drop index if exists extractions_cache_key_idx;
alter table extractions add constraint extractions_cache_key_key unique (cache_key);
drop table if exists llm_cache;
