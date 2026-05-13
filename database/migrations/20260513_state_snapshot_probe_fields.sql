-- Runtime-state and analytics fields for safer stateless scheduled execution.

alter table if exists open_trades
    add column if not exists atr_pct numeric(8,4),
    add column if not exists atr_raw numeric(12,4);

alter table if exists portfolio_snapshots
    add column if not exists broker_equity_usd numeric(14,2),
    add column if not exists broker_cash_usd numeric(14,2),
    add column if not exists effective_equity_usd numeric(14,2),
    add column if not exists effective_cash_usd numeric(14,2),
    add column if not exists open_market_value_usd numeric(14,2),
    add column if not exists gross_market_value_usd numeric(14,2),
    add column if not exists unrealized_pnl_usd numeric(14,2),
    add column if not exists unrealized_pnl_eur numeric(14,2),
    add column if not exists fx_rate numeric(10,6),
    add column if not exists capital_ceiling_eur numeric(14,2),
    add column if not exists capital_ceiling_usd numeric(14,2);

alter table if exists blocked_opportunities
    add column if not exists minutes_since_open smallint,
    add column if not exists atr_pct numeric(8,4),
    add column if not exists volatility_bucket text,
    add column if not exists is_leveraged_etf boolean default false,
    add column if not exists spread_pct numeric(8,4),
    add column if not exists opening_range_position numeric(8,4),
    add column if not exists probe_eligible boolean default false,
    add column if not exists reason_not_probed text;

create index if not exists idx_blocked_opportunities_probe_context
    on blocked_opportunities (probe_eligible, minutes_since_open, created_at desc);

