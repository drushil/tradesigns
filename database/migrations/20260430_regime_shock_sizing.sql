-- Regime state, macro shock scanner, ATR sizing, and mean-reversion trade flags.

begin;

alter table if exists signals
    add column if not exists regime_bull_bear text,
    add column if not exists shock_detected boolean default false,
    add column if not exists shock_classification text;

alter table if exists trades
    add column if not exists sizing_json jsonb,
    add column if not exists mean_reversion_trade boolean default false,
    add column if not exists swing_trade boolean default false;

alter table if exists open_trades
    add column if not exists sizing_json jsonb,
    add column if not exists mean_reversion_trade boolean default false,
    add column if not exists swing_trade boolean default false;

create index if not exists idx_signals_regime_shock_time
    on signals (regime_bull_bear, shock_detected, created_at desc);

create index if not exists idx_trades_sizing_flags_time
    on trades (mean_reversion_trade, swing_trade, created_at desc);

commit;
