-- Phase 1 migration — Bollinger squeeze, Put/Call ratio, ATR
-- Run once in: Supabase → SQL Editor → New Query → Run
-- All statements are idempotent (IF NOT EXISTS / IF EXISTS).

-- 1. Add new signal score columns to the signals table
alter table if exists signals
    add column if not exists bollinger_score  real,
    add column if not exists put_call_score   real,
    add column if not exists atr_pct          numeric(6,4);

-- 2. Add new weight columns to the signal_weights table
alter table if exists signal_weights
    add column if not exists bollinger_squeeze numeric(6,4) not null default 0.09
        check (bollinger_squeeze between 0 and 1),
    add column if not exists put_call_ratio    numeric(6,4) not null default 0.05
        check (put_call_ratio    between 0 and 1);

-- 3. Rebuild latest_signal_weights view to include new columns.
--    DROP + CREATE required because CREATE OR REPLACE cannot reorder columns
--    (existing view may be missing macd_crossover / relative_strength).
drop view if exists latest_signal_weights;

create view latest_signal_weights as
select distinct on (regime)
    regime,
    order_book,
    tape_aggression,
    rsi_divergence,
    news_sentiment,
    vwap_deviation,
    coalesce(macd_crossover,   0.10) as macd_crossover,
    coalesce(relative_strength, 0.08) as relative_strength,
    coalesce(bollinger_squeeze, 0.09) as bollinger_squeeze,
    coalesce(put_call_ratio,    0.05) as put_call_ratio,
    trade_count,
    updated_at
from signal_weights
order by regime, updated_at desc;

grant select on latest_signal_weights to anon;
