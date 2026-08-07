-- Advisory-auto risk controls and exact simulator/paper pairing metadata.
-- Idempotent so it can be applied safely to existing Supabase projects.

alter table if exists advisory_signals
    add column if not exists auto_client_order_id text,
    add column if not exists auto_limit_price numeric(14,4),
    add column if not exists auto_planned_risk_eur numeric(10,2),
    add column if not exists auto_entry_policy text,
    add column if not exists auto_submitted_qty numeric(14,6);

alter table if exists trades
    add column if not exists entry_policy text,
    add column if not exists intended_entry_price numeric(14,4),
    add column if not exists entry_slippage_pct numeric(8,4),
    add column if not exists planned_risk_eur numeric(10,2);

create index if not exists idx_trades_advisory_pairing
    on trades (advisory_signal_id, entry_policy, created_at desc)
    where trade_source = 'advisory_auto' and advisory_signal_id is not null;
