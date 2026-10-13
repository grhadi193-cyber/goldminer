-- 0002_views: reporting views (leaderboard = latest row per channel, coverage helpers).

-- migrate:up

-- Leaderboard: latest channel_scores row per (channel, horizon='eod').
create or replace view channel_leaderboard as
select distinct on (cs.channel_id)
       cs.id                    as score_id,
       c.id                     as channel_id,
       c.username,
       c.title,
       c.active,
       c.tier,
       cs.asof_date,
       cs.horizon,
       cs.label,
       cs.lead_lag_minutes,
       cs.lift_bps_30m,
       cs.lift_bps_1h,
       cs.lift_bps_3h,
       cs.lift_bps_eod,
       cs.net_of_costs,
       cs.n_events,
       cs.ci_low_bps,
       cs.ci_high_bps,
       cs.fdr_corrected,
       cs.q_value,
       cs.pumper_flag,
       cs.quoted_bias_pct,
       cs.method_version,
       cs.computed_at
from channel_scores cs
join channels c on c.id = cs.channel_id
where cs.horizon = 'eod'
order by cs.channel_id, cs.asof_date desc, cs.computed_at desc;

-- Coverage: bars per series per month, gaps per series per month (R8).
create or replace view price_coverage_monthly as
with bars as (
    select source,
           symbol,
           resolution,
           date_trunc('month', ts)::date as month,
           count(*)                      as n_bars,
           min(ts)                       as first_ts,
           max(ts)                       as last_ts
    from price_bars
    group by 1, 2, 3, 4
),
gaps as (
    select source,
           symbol,
           date_trunc('month', from_ts)::date as month,
           count(*)                           as n_gaps,
           sum(extract(epoch from (to_ts - from_ts))) as gap_seconds
    from price_gaps
    group by 1, 2, 3
)
select b.source,
       b.symbol,
       b.resolution,
       b.month,
       b.n_bars,
       b.first_ts,
       b.last_ts,
       coalesce(g.n_gaps, 0)      as n_gaps,
       coalesce(g.gap_seconds, 0) as gap_seconds
from bars b
left join gaps g
       on g.source = b.source
      and g.symbol = b.symbol
      and g.month = b.month;

-- migrate:down

drop view if exists price_coverage_monthly;
drop view if exists channel_leaderboard;
