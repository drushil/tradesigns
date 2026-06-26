-- Keep paper-execution journal constraints aligned with executor exit reasons,
-- and quarantine historical chase simulations with impossible target geometry.

alter table trades
    drop constraint if exists trades_exit_reason_check;
do $$
declare
    cname text;
begin
    select conname into cname
    from pg_constraint
    where conrelid = 'trades'::regclass
      and contype = 'c'
      and pg_get_constraintdef(oid) like '%exit_reason%';
    if cname is not null then
        execute format('alter table trades drop constraint %I', cname);
    end if;
exception when others then null;
end $$;

alter table trades
    add constraint trades_exit_reason_check check (exit_reason in (
        'stop_loss','take_profit','time_exit',
        'signal_reversal','manual','circuit_breaker',
        'chandelier_stop','swing_exit','earnings_tomorrow',
        'regime_turned_bear','momentum_reversed',
        'macro_shock','take_profit_8pct','swing_promoted',
        'momentum_peak_decay','eod_cleanup',
        'thesis_invalidated','partial_runner_stop','a_plus_override',
        'stale_no_position','leveraged_etf_time_exit',
        'near_t1_protection','eod_flat','orphan_flatten'
    ));

update advisory_auto_simulations
set status = 'expired',
    closure_reason = 'invalid_chase_geometry',
    r_multiple = null,
    notes = coalesce(notes, '{}'::jsonb)
        || '{"invalid_chase_geometry": true}'::jsonb
where mode = 'chase_tracker'
  and status in ('hit_target_1', 'hit_target_2')
  and fill_price >= target_1;
