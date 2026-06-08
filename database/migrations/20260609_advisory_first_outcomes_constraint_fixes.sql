-- Follow-up to 20260608_advisory_first_outcomes.sql
--
-- Fixes two CHECK constraints that would have blocked new status values:
--
-- 1. advisory_auto_simulations.status — extends to include the three new EOD
--    statuses introduced by the nightly EOD closer: closed_eod_win, closed_eod_loss,
--    closed_eod (legacy pre-migration rows).
--
-- 2. trades.trade_source — extends to include advisory_paper and advisory_live,
--    the values that will be used when advisory dry-run graduates to paper/live
--    trading (advisory_execution_scoreboard already unions on these values).

alter table advisory_auto_simulations
    drop constraint if exists advisory_auto_simulations_status_check;
alter table advisory_auto_simulations
    add constraint advisory_auto_simulations_status_check
    check (status in (
        'pending', 'filled', 'expired',
        'hit_stop', 'hit_target_1', 'hit_target_2',
        'cancelled_signal_weak', 'hit_near_t1_protection',
        'closed_eod', 'closed_eod_win', 'closed_eod_loss'
    ));

alter table trades
    drop constraint if exists trades_trade_source_check;
do $$
declare
    cname text;
begin
    select conname into cname
    from pg_constraint
    where conrelid = 'trades'::regclass
      and contype = 'c'
      and pg_get_constraintdef(oid) like '%trade_source%';
    if cname is not null then
        execute format('alter table trades drop constraint %I', cname);
    end if;
exception when others then null;
end $$;
alter table trades
    add constraint trades_trade_source_check
    check (trade_source in (
        'agent',
        'advisory_manual',
        'advisory_auto',
        'advisory_paper',
        'advisory_live',
        'manual_other'
    ));
