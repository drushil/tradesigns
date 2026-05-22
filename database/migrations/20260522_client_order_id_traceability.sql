-- Persist Alpaca client_order_id for broker/database traceability.
alter table if exists open_trades
    add column if not exists client_order_id text;

alter table if exists trades
    add column if not exists client_order_id text;

create index if not exists idx_open_trades_client_order_id
    on open_trades (client_order_id)
    where client_order_id is not null;

create index if not exists idx_trades_client_order_id
    on trades (client_order_id)
    where client_order_id is not null;
