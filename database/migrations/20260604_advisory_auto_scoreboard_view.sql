-- Two-track scoreboard: compare strict trade-stage dry-run (advisory_signals.auto_*)
-- against the watch-limit simulator (advisory_auto_simulations) on the same axes.
-- One row per (utc_date, grade); presence on either side is enough to materialise.

create or replace view advisory_auto_scoreboard as
with strict as (
    select
        date_trunc('day', auto_checked_at)::date as utc_date,
        coalesce(grade, '-') as grade,
        count(*) as strict_checked,
        count(*) filter (where auto_status = 'eligible') as strict_eligible,
        count(*) filter (where auto_status = 'skipped') as strict_skipped,
        count(*) filter (where auto_status = 'submitted') as strict_submitted,
        count(*) filter (where auto_status = 'filled') as strict_filled,
        count(*) filter (where auto_skip_reason like 'skipped_stage_not_trade%') as strict_skip_watch,
        count(*) filter (where auto_skip_reason like 'skipped_chase%') as strict_skip_chase,
        count(*) filter (where auto_skip_reason like 'skipped_price_outside_band%') as strict_skip_band,
        count(*) filter (where auto_skip_reason like 'skipped_invalid_levels%') as strict_skip_levels,
        count(*) filter (where auto_skip_reason like 'skipped_expired%') as strict_skip_expired
    from advisory_signals
    where auto_checked_at is not null
      and side = 'BUY'
      and market = 'US'
    group by 1, 2
),
sim as (
    select
        date_trunc('day', simulated_at)::date as utc_date,
        coalesce(grade, '-') as grade,
        count(*) as sim_total,
        count(*) filter (where status in ('filled','hit_stop','hit_target_1','hit_target_2')) as sim_filled_or_closed,
        count(*) filter (where status = 'expired') as sim_expired,
        count(*) filter (where status = 'pending') as sim_pending,
        count(*) filter (where status = 'hit_stop') as sim_hit_stop,
        count(*) filter (where status = 'hit_target_1') as sim_hit_t1,
        count(*) filter (where status = 'hit_target_2') as sim_hit_t2,
        round(avg(mfe_pct) filter (where mfe_pct is not null)::numeric, 3) as sim_avg_mfe_pct,
        round(avg(mae_pct) filter (where mae_pct is not null)::numeric, 3) as sim_avg_mae_pct
    from advisory_auto_simulations
    where side = 'BUY'
      and market = 'US'
    group by 1, 2
)
select
    coalesce(strict.utc_date, sim.utc_date) as utc_date,
    coalesce(strict.grade, sim.grade) as grade,
    coalesce(strict.strict_checked, 0) as strict_checked,
    coalesce(strict.strict_eligible, 0) as strict_eligible,
    coalesce(strict.strict_skipped, 0) as strict_skipped,
    coalesce(strict.strict_skip_watch, 0) as strict_skip_watch,
    coalesce(strict.strict_skip_chase, 0) as strict_skip_chase,
    coalesce(strict.strict_skip_band, 0) as strict_skip_band,
    coalesce(strict.strict_skip_levels, 0) as strict_skip_levels,
    coalesce(strict.strict_skip_expired, 0) as strict_skip_expired,
    coalesce(sim.sim_total, 0) as sim_total,
    coalesce(sim.sim_filled_or_closed, 0) as sim_filled_or_closed,
    coalesce(sim.sim_hit_stop, 0) as sim_hit_stop,
    coalesce(sim.sim_hit_t1, 0) as sim_hit_t1,
    coalesce(sim.sim_hit_t2, 0) as sim_hit_t2,
    coalesce(sim.sim_expired, 0) as sim_expired,
    coalesce(sim.sim_pending, 0) as sim_pending,
    sim.sim_avg_mfe_pct,
    sim.sim_avg_mae_pct,
    -- Edge proxies
    case when coalesce(sim.sim_hit_stop, 0) + coalesce(sim.sim_hit_t1, 0) + coalesce(sim.sim_hit_t2, 0) > 0
         then round(
             (coalesce(sim.sim_hit_t1, 0) + coalesce(sim.sim_hit_t2, 0))::numeric
             / (coalesce(sim.sim_hit_stop, 0) + coalesce(sim.sim_hit_t1, 0) + coalesce(sim.sim_hit_t2, 0))
             * 100, 1)
    end as sim_target_win_pct
from strict
full outer join sim using (utc_date, grade)
order by utc_date desc, grade;

grant select on advisory_auto_scoreboard to anon;
