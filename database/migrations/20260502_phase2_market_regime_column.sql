-- Phase 2 gap fix: add market_regime column to signals table.
-- regime_bull_bear is kept for backwards compatibility — do not drop it.
-- Run manually in Supabase SQL Editor.

begin;

alter table if exists signals
    add column if not exists market_regime text;

create index if not exists idx_signals_market_regime
    on signals (market_regime, created_at desc)
    where market_regime is not null;

commit;
