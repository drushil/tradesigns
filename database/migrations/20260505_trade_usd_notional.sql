-- Track USD order notional separately from EUR reporting notional.
-- Alpaca prices and quantities are USD-denominated; dashboard P&L remains EUR.

alter table if exists trades
    add column if not exists size_usd numeric(10,2);

alter table if exists open_trades
    add column if not exists size_usd numeric(10,2);
