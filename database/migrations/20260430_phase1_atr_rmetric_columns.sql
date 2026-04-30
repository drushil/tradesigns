-- Phase 1 completion: ATR volatility regime, R-multiples, stop tracking.
-- Run manually in Supabase SQL Editor.

begin;

alter table if exists signals
    add column if not exists atr_stop_pct      numeric(6,4),
    add column if not exists volatility_regime text;

alter table if exists trades
    add column if not exists atr_at_entry  numeric(6,4),
    add column if not exists r_multiple    numeric(8,4),
    add column if not exists stop_pct_used numeric(6,4);

create index if not exists idx_signals_volatility_regime
    on signals (volatility_regime, created_at desc)
    where volatility_regime is not null;

create index if not exists idx_trades_r_multiple
    on trades (r_multiple)
    where r_multiple is not null;

commit;
