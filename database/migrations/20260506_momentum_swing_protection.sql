-- Persist promoted momentum-swing management state and broker protection IDs.

alter table if exists trades
    add column if not exists swing_conviction numeric(4,3),
    add column if not exists swing_reasons text[],
    add column if not exists hold_days_actual smallint,
    add column if not exists stop_multiplier numeric(4,2),
    add column if not exists daily_reeval_count smallint default 0,
    add column if not exists exit_trigger text,
    add column if not exists promoted_to_swing boolean,
    add column if not exists promoted_at timestamptz,
    add column if not exists initial_horizon text,
    add column if not exists trailing_stop_price numeric(12,4),
    add column if not exists highest_price_since_entry numeric(12,4),
    add column if not exists overnight_gap_pct numeric(8,4),
    add column if not exists protective_stop_order_id text,
    add column if not exists hold_decision_json jsonb;

alter table if exists signals
    add column if not exists swing_detected boolean,
    add column if not exists swing_conviction numeric(4,3),
    add column if not exists swing_hold_days smallint;

alter table if exists open_trades
    add column if not exists promoted_to_swing boolean default false,
    add column if not exists promoted_at timestamptz,
    add column if not exists swing_conviction numeric(4,3),
    add column if not exists swing_reasons text[],
    add column if not exists highest_price_since_entry numeric(12,4),
    add column if not exists trailing_stop_price numeric(12,4),
    add column if not exists stop_multiplier numeric(4,2),
    add column if not exists stop_pct numeric(6,4),
    add column if not exists max_hold_minutes smallint,
    add column if not exists daily_reeval_count smallint default 0,
    add column if not exists hold_decision_json jsonb,
    add column if not exists initial_horizon text,
    add column if not exists protective_stop_order_id text;

alter table if exists trades drop constraint if exists trades_exit_reason_check;
alter table if exists trades
    add constraint trades_exit_reason_check check (exit_reason in (
        'stop_loss','take_profit','time_exit',
        'signal_reversal','manual','circuit_breaker',
        'chandelier_stop','swing_exit','earnings_tomorrow',
        'regime_turned_bear','momentum_reversed',
        'macro_shock','take_profit_8pct','swing_promoted'
    ));
