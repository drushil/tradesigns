-- Swing trading, macro regime, and extreme dip scanner support.

begin;

alter table if exists signals
    add column if not exists macro_regime text
        check (macro_regime in ('geopolitical_shock','rate_shift','normal','risk_off','risk_on')),
    add column if not exists macro_multiplier numeric(5,2);

alter table if exists open_trades
    add column if not exists hold_days smallint,
    add column if not exists horizon text
        check (horizon in ('short','mid','both','swing','intraday')),
    add column if not exists macro_regime text,
    add column if not exists macro_multiplier numeric(5,2),
    add column if not exists dip_type text;

alter table if exists trades
    add column if not exists macro_regime text
        check (macro_regime in ('geopolitical_shock','rate_shift','normal','risk_off','risk_on')),
    add column if not exists macro_multiplier numeric(5,2),
    add column if not exists dip_type text;

alter table if exists trades
    drop constraint if exists trades_horizon_check;

alter table if exists trades
    add constraint trades_horizon_check
    check (horizon in ('short','mid','both','swing','intraday'));

create index if not exists idx_signals_macro_time
    on signals (macro_regime, created_at desc);

create index if not exists idx_open_trades_horizon_status
    on open_trades (horizon, status, created_at desc);

create index if not exists idx_trades_horizon_time
    on trades (horizon, created_at desc);

commit;
