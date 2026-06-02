"""
scripts/record_manual_trade.py
Record a manually-executed real-money trade into the trades table.
Usage: python scripts/record_manual_trade.py
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

from database.client import get_client


def record_manual_trade(
    ticker: str,
    side: str,              # 'BUY' for long position (even if closing via sell)
    exit_price: float,
    entry_price: float,
    quantity: float,
    pnl_eur: float,
    pnl_pct: float,
    net_pnl_pct: float,
    size_eur: float,        # net proceeds
    commission_eur: float,
    exit_time: datetime,
    notes: str = "",
    advisory_signal_id: int = None,
) -> dict:
    db = get_client(write=True)
    record = {
        "ticker":             ticker,
        "side":               side,
        "entry_price":        round(entry_price, 4),
        "exit_price":         round(exit_price, 4),
        "quantity":           round(quantity, 6),
        "size_eur":           round(size_eur, 2),
        "pnl_eur":            round(pnl_eur, 2),
        "pnl_pct":            round(pnl_pct, 4),
        "net_pnl_pct":        round(net_pnl_pct, 4),
        "commission_eur":     round(commission_eur, 4),
        "exit_time":          exit_time.isoformat(),
        "exit_reason":        "manual",
        "trade_source":       "advisory_manual",
        "llm_rationale":      notes or f"Manual trade via Trade Republic. Profit {pnl_pct}%.",
        "order_id":           f"MANUAL-{ticker}-{exit_time.strftime('%Y%m%d')}",
    }
    if advisory_signal_id:
        record["advisory_signal_id"] = advisory_signal_id
    result = db.table("trades").insert(record).execute()
    return result.data[0] if result.data else {}


if __name__ == "__main__":
    exit_dt = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)  # approximate; adjust if known

    trades = [
        dict(
            ticker="NVDA",
            side="BUY",
            exit_price=189.36,
            entry_price=185.3965,
            quantity=20.0,
            pnl_eur=78.27,
            pnl_pct=2.1100,
            net_pnl_pct=2.0839,
            size_eur=3786.20,
            commission_eur=1.00,
            exit_time=exit_dt,
            notes="Manual sell via Trade Republic. 20 shares NVDA @ 189.36 EUR. +2.11% / +78.27 EUR.",
        ),
        dict(
            ticker="MU",
            side="BUY",
            exit_price=886.90,
            entry_price=876.1753,
            quantity=5.707762,
            pnl_eur=60.21,
            pnl_pct=1.2000,
            net_pnl_pct=1.1840,
            size_eur=5061.21,
            commission_eur=1.00,
            exit_time=exit_dt,
            notes="Manual sell via Trade Republic. 5.707762 shares MU @ 886.90 EUR. +1.2% / +60.21 EUR.",
        ),
    ]

    for t in trades:
        try:
            row = record_manual_trade(**t)
            print(f"✓ Recorded {t['ticker']}: id={row.get('id')} pnl={t['pnl_eur']}€")
        except Exception as e:
            print(f"✗ Failed {t['ticker']}: {e}")
