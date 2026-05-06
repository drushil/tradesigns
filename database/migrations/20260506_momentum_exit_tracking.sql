-- Momentum exit tracking fields for extended intraday trades.
-- These enable peak-decay, VWAP-recross, and score-persistence exits.

alter table if exists open_trades
    add column if not exists peak_directional_score  numeric(6,4)  default 0,
    add column if not exists entry_vwap              numeric(12,4) default 0,
    add column if not exists consecutive_weak_cycles smallint      default 0;

-- Extend exit_reason constraint with momentum-exit values
alter table if exists trades drop constraint if exists trades_exit_reason_check;
alter table if exists trades
    add constraint trades_exit_reason_check check (exit_reason in (
        'stop_loss','take_profit','time_exit',
        'signal_reversal','manual','circuit_breaker',
        'chandelier_stop','swing_exit','earnings_tomorrow',
        'regime_turned_bear','momentum_reversed',
        'macro_shock','take_profit_8pct','swing_promoted',
        'momentum_peak_decay','vwap_recross','score_persistence_exit'
    ));
