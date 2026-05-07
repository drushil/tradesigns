-- Expose yield curve state in the signals table for dashboard filtering.
-- regime_debug_json (jsonb) already captures yield_curve + yield_curve_state
-- automatically via RegimeState.to_dict() — no migration needed for that.
-- This optional column allows direct SQL queries on yield curve state.

alter table if exists signals
    add column if not exists yield_curve_state text
        check (yield_curve_state in ('inverted', 'flat', 'normal'));

alter table if exists signals
    add column if not exists yield_curve numeric(6,3);

comment on column signals.yield_curve is
    'T10Y2Y spread from FRED (10yr minus 2yr Treasury, %). Negative = inverted.';
