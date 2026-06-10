-- Fix execution scoreboard accounting for legacy/bare EOD simulation closes.
-- The simulator now tries harder to mark EOD rows as closed_eod_win/loss, but
-- existing or data-starved closed_eod rows should still count as closed.

create or replace view advisory_execution_scoreboard as
with sim as (
    select
        date_trunc('day', simulated_at)::date as utc_date,
        market,
        coalesce(entry_policy, mode, 'unknown') as entry_policy,
        coalesce(grade, '-')                    as grade,
        side,
        'simulation'::text                      as source,
        count(*)                                as total,
        count(*) filter (
            where status in (
                'filled',
                'hit_stop',
                'hit_target_1',
                'hit_target_2',
                'closed_eod',
                'closed_eod_win',
                'closed_eod_loss'
            )
        )                                       as filled_or_closed,
        count(*) filter (where closure_reason = 'target_1')  as tp1_hit,
        count(*) filter (where closure_reason = 'target_2')  as tp2_hit,
        count(*) filter (where closure_reason = 'stop')      as stopped,
        count(*) filter (where closure_reason = 'eod_close') as eod_closed,
        count(*) filter (where status = 'expired')           as expired,
        count(*) filter (where status = 'cancelled_signal_weak') as cancelled_weak,
        round(avg(r_multiple)::numeric, 3)                   as avg_r,
        round(avg(mfe_pct)::numeric, 3)                      as avg_mfe_pct,
        round(avg(mae_pct)::numeric, 3)                      as avg_mae_pct,
        round(avg(entry_policy_quality)::numeric, 3)         as avg_entry_quality
    from advisory_auto_simulations
    where simulated_at >= now() - interval '90 days'
    group by 1, 2, 3, 4, 5
),
real_trades as (
    select
        date_trunc('day', coalesce(entry_time, created_at))::date as utc_date,
        'US'::text                              as market,
        'real'::text                            as entry_policy,
        coalesce(setup_grade, '-')              as grade,
        side,
        trade_source                            as source,
        count(*)                                as total,
        count(*) filter (where exit_price is not null) as filled_or_closed,
        count(*) filter (where exit_reason ilike '%target%' or exit_reason ilike '%t1%') as tp1_hit,
        0                                       as tp2_hit,
        count(*) filter (where exit_reason ilike '%stop%') as stopped,
        count(*) filter (where exit_reason ilike '%eod%')  as eod_closed,
        0                                       as expired,
        0                                       as cancelled_weak,
        round(avg(r_multiple)::numeric, 3)      as avg_r,
        null::numeric                           as avg_mfe_pct,
        null::numeric                           as avg_mae_pct,
        null::numeric                           as avg_entry_quality
    from trades
    where trade_source like 'advisory%'
      and coalesce(entry_time, created_at) >= now() - interval '90 days'
    group by 1, 2, 3, 4, 5, 6
)
select * from sim
union all
select * from real_trades;
