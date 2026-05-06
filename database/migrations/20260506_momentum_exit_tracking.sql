-- Momentum exit tracking: peak_directional_score for extended intraday trades.
-- VWAP recross and score persistence deferred until validated by trade data.

alter table if exists open_trades
    add column if not exists peak_directional_score numeric(6,4) default 0;

-- Extend exit_reason constraint with momentum_peak_decay
alter table if exists trades drop constraint if exists trades_exit_reason_check;
alter table if exists trades
    add constraint trades_exit_reason_check check (exit_reason in (
        'stop_loss','take_profit','time_exit',
        'signal_reversal','manual','circuit_breaker',
        'chandelier_stop','swing_exit','earnings_tomorrow',
        'regime_turned_bear','momentum_reversed',
        'macro_shock','take_profit_8pct','swing_promoted',
        'momentum_peak_decay'
    ));
