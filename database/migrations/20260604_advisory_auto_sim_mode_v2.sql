-- Simulator V2: mode tagging + extended terminal vocabulary.
--
-- mode distinguishes the measurement scenario a sim row represents:
--   trade_now             — limit at entry band on a trade-stage signal (enter now)
--   watch_pullback        — limit at entry band on a watch-stage signal (wait for dip)
--   chase_tracker         — synthetic fill at the chase price the executor refused
--   momentum_continuation — reserved (not yet emitted)
--
-- A single advisory_signal can now back more than one sim (e.g. the patient
-- limit-at-band trade_now sim AND the chase_tracker sim that bought higher),
-- so uniqueness moves from (advisory_signal_id) to (advisory_signal_id, mode).
--
-- Two new terminal statuses:
--   cancelled_signal_weak  — pending limit pulled because conviction died
--   hit_near_t1_protection — captured most of the T1 move then retraced; booked

alter table advisory_auto_simulations
    add column if not exists mode text not null default 'watch_pullback';

update advisory_auto_simulations set mode = 'watch_pullback' where mode is null;

alter table advisory_auto_simulations
    drop constraint if exists advisory_auto_sim_mode_check;
alter table advisory_auto_simulations
    add constraint advisory_auto_sim_mode_check
    check (mode in ('trade_now', 'watch_pullback', 'chase_tracker', 'momentum_continuation'));

alter table advisory_auto_simulations
    drop constraint if exists advisory_auto_simulations_status_check;
alter table advisory_auto_simulations
    add constraint advisory_auto_simulations_status_check
    check (status in (
        'pending', 'filled', 'expired',
        'hit_stop', 'hit_target_1', 'hit_target_2',
        'cancelled_signal_weak', 'hit_near_t1_protection'
    ));

alter table advisory_auto_simulations
    drop constraint if exists advisory_auto_simulations_advisory_signal_id_key;
alter table advisory_auto_simulations
    drop constraint if exists advisory_auto_sim_signal_mode_key;
alter table advisory_auto_simulations
    add constraint advisory_auto_sim_signal_mode_key
    unique (advisory_signal_id, mode);

create index if not exists idx_advisory_auto_sim_mode
    on advisory_auto_simulations (mode, status, simulated_at desc);
