-- Migration: advisory-auto dry-run lifecycle columns on advisory_signals
-- Run in Supabase SQL Editor after 20260602_trade_source_advisory.sql

alter table advisory_signals
    add column if not exists auto_status text
        check (auto_status in ('eligible','skipped','submitted','filled','closed','rejected','cancelled')),
    add column if not exists auto_checked_at timestamptz,
    add column if not exists auto_skip_reason text,
    add column if not exists auto_order_id text,
    add column if not exists auto_fill_price numeric(12,4),
    add column if not exists auto_fill_qty   numeric(14,6),
    add column if not exists auto_pnl_eur    numeric(10,2),
    add column if not exists auto_exit_reason text;

create index if not exists idx_advisory_signals_auto_status
    on advisory_signals (auto_status, created_at desc)
    where auto_status is not null;
