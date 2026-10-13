-- 0005: store the deterministic gate verdict alongside each Jev decision.
--
-- Rationale: the UI must show the verdict the ENGINE produced, not one recomputed in a
-- browser or in an export script. Storing it also makes "what did the gates decide that day,
-- and why" answerable without re-running the replay.

-- migrate:up
alter table jev_decisions add column if not exists verdict_json jsonb;
alter table jev_decisions add column if not exists day_pnl_bps double precision;
alter table jev_decisions add column if not exists confidence double precision;

create index if not exists jev_decisions_verdict_idx on jev_decisions ((verdict_json->>'action'));

-- migrate:down
drop index if exists jev_decisions_verdict_idx;
alter table jev_decisions drop column if exists confidence;
alter table jev_decisions drop column if exists day_pnl_bps;
alter table jev_decisions drop column if exists verdict_json;
