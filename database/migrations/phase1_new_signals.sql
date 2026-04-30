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

-- 3. Rebuild latest_signal_weights view to include new columns
create or replace view latest_signal_weights as
select distinct on (regime)
    regime, order_book, tape_aggression, rsi_divergence,
    news_sentiment, vwap_deviation, macd_crossover, relative_strength,
    bollinger_squeeze, put_call_ratio,
    trade_count, updated_at
from signal_weights
order by regime, updated_at desc;

grant select on latest_signal_weights to anon;
