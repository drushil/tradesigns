-- Learning/execution optimization migration.
-- Run this manually in Supabase SQL Editor. Codex intentionally did not execute it.

begin;

-- Store execution context needed for reconciliation and R-multiple analysis.
alter table if exists trades
    add column if not exists stop_price numeric(12,4),
    add column if not exists take_profit_price numeric(12,4),
    add column if not exists order_id text,
    add column if not exists close_order_id text,
    add column if not exists close_error text;

-- Persist enough open-trade state for short-lived scheduler runs to resume
-- time exits and record position quantity accurately.
alter table if exists open_trades
    add column if not exists quantity numeric(14,6),
    add column if not exists hold_minutes smallint;

-- Ensure all currently computed signals are stored by the agent and dashboard.
alter table if exists signals
    add column if not exists bollinger_score real,
    add column if not exists put_call_score real,
    add column if not exists atr_pct numeric(6,4);

-- Ensure learned weights cover all weighted signals.
alter table if exists signal_weights
    add column if not exists bollinger_squeeze numeric(6,4) not null default 0.09
        check (bollinger_squeeze between 0 and 1),
    add column if not exists put_call_ratio numeric(6,4) not null default 0.05
        check (put_call_ratio between 0 and 1);

create index if not exists idx_trades_order_id
    on trades (order_id)
    where order_id is not null;

create index if not exists idx_trades_profile_time
    on trades (risk_profile, created_at desc);

create index if not exists idx_signals_ticker_composite_time
    on signals (ticker, composite_score, created_at desc);

create or replace view trade_stats_30d as
select
    count(*)                                                    as total_trades,
    count(*) filter (where net_pnl_pct > 0)                    as wins,
    count(*) filter (where net_pnl_pct <= 0)                   as losses,
    round(count(*) filter (where net_pnl_pct > 0)::numeric
          / nullif(count(*), 0) * 100, 1)                      as win_rate_pct,
    round(avg(net_pnl_pct), 4)                                 as avg_net_pnl_pct,
    round(sum(pnl_eur), 2)                                     as total_pnl_eur,
    round(avg(hold_minutes), 1)                                as avg_hold_minutes,
    round(sum(commission_eur + slippage_eur + llm_cost_eur), 4) as total_costs_eur
from trades
where created_at >= now() - interval '30 days';

grant select on trade_stats_30d to anon;

drop view if exists latest_signal_weights;

create view latest_signal_weights as
select distinct on (regime)
    regime,
    order_book,
    tape_aggression,
    rsi_divergence,
    news_sentiment,
    vwap_deviation,
    coalesce(macd_crossover, 0.10) as macd_crossover,
    coalesce(relative_strength, 0.08) as relative_strength,
    coalesce(bollinger_squeeze, 0.09) as bollinger_squeeze,
    coalesce(put_call_ratio, 0.05) as put_call_ratio,
    trade_count,
    updated_at
from signal_weights
order by regime, updated_at desc;

grant select on latest_signal_weights to anon;

commit;
