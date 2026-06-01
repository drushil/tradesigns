-- Manual trades executed 2026-06-01 via Trade Republic
-- Run in Supabase SQL Editor as an alternative to record_manual_trade.py

INSERT INTO trades (
    ticker, side,
    entry_price, exit_price, quantity,
    size_eur, pnl_eur, pnl_pct, net_pnl_pct,
    commission_eur,
    exit_time, exit_reason,
    order_id, llm_rationale
) VALUES
(
    'NVDA', 'BUY',
    185.3965, 189.36, 20.0,
    3786.20, 78.27, 2.1100, 2.0839,
    1.00,
    '2026-06-01T12:00:00+00:00', 'manual',
    'MANUAL-NVDA-20260601',
    'Manual sell via Trade Republic. 20 shares @ 189.36 EUR. +2.11% / +78.27 EUR.'
),
(
    'MU', 'BUY',
    876.1753, 886.90, 5.707762,
    5061.21, 60.21, 1.2000, 1.1840,
    1.00,
    '2026-06-01T12:00:00+00:00', 'manual',
    'MANUAL-MU-20260601',
    'Manual sell via Trade Republic. 5.707762 shares @ 886.90 EUR. +1.2% / +60.21 EUR.'
);
