-- Migration: add trade_source discriminator and advisory_signal_id FK to trades
-- Run in Supabase SQL Editor

alter table trades
    add column if not exists trade_source text
        check (trade_source in ('agent', 'advisory_manual', 'manual_other'))
        default 'agent',
    add column if not exists advisory_signal_id bigint
        references advisory_signals(id) on delete set null;

create index if not exists idx_trades_trade_source_created_at
    on trades (trade_source, created_at desc);

create index if not exists idx_trades_advisory_signal_id
    on trades (advisory_signal_id)
    where advisory_signal_id is not null;

-- Backfill existing manually-recorded trades (NVDA, MU 2026-06-01)
update trades
set trade_source = 'advisory_manual'
where order_id like 'MANUAL-%'
  and coalesce(trade_source, '') <> 'advisory_manual';
