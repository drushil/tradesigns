-- Track intended vs executed sizing so analytics use actual exposure
-- after bracket-order whole-share rounding.

alter table if exists trades
    add column if not exists submitted_qty numeric(14,6),
    add column if not exists implied_qty numeric(14,6),
    add column if not exists intended_size_eur numeric(10,2),
    add column if not exists executed_size_eur numeric(10,2),
    add column if not exists executed_size_usd numeric(10,2),
    add column if not exists bracket_floor_qty_loss_pct numeric(8,4);

alter table if exists open_trades
    add column if not exists submitted_qty numeric(14,6),
    add column if not exists implied_qty numeric(14,6),
    add column if not exists intended_size_eur numeric(10,2),
    add column if not exists executed_size_eur numeric(10,2),
    add column if not exists executed_size_usd numeric(10,2),
    add column if not exists bracket_floor_qty_loss_pct numeric(8,4);
