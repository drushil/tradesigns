-- Trade diagnostics for learning quality:
-- - exposure_direction separates bullish market exposure from inverse ETF / short exposure
-- - strategy_family groups outcomes by strategy intent
-- - regime_debug_json records why the regime detector classified each trade/signal

alter table if exists trades
    add column if not exists exposure_direction text,
    add column if not exists strategy_family text,
    add column if not exists regime_debug_json jsonb;

alter table if exists open_trades
    add column if not exists exposure_direction text,
    add column if not exists strategy_family text,
    add column if not exists regime_debug_json jsonb;

alter table if exists signals
    add column if not exists action_hint text,
    add column if not exists exposure_direction text,
    add column if not exists strategy_family text,
    add column if not exists regime_debug_json jsonb;

create index if not exists idx_trades_exposure_direction
    on trades (exposure_direction, created_at desc);

create index if not exists idx_trades_strategy_family
    on trades (strategy_family, created_at desc);
