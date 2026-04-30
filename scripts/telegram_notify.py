"""
scripts/telegram_notify.py
Sends Telegram cycle summaries and extreme-dip alerts.
"""
import os
from datetime import datetime

import requests


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def send_message(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram not configured - skipping notification")
        return False
    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
        timeout=10,
    )
    if not resp.ok:
        print(f"Telegram send failed: {resp.status_code} {resp.text[:200]}")
    return resp.ok


def _recent_logged_dips() -> list[dict]:
    try:
        from database.client import get_logs

        logs = get_logs(level="SIGNAL", limit=100)
        today = datetime.utcnow().date().isoformat()
        return [
            l.get("detail") or {}
            for l in logs
            if l.get("event") == "extreme_dip_detected"
            and str(l.get("logged_at", "")).startswith(today)
        ]
    except Exception as e:
        print(f"Could not load dip logs: {str(e)[:120]}")
        return []


def _live_dip_scan() -> list[dict]:
    if os.getenv("TELEGRAM_SCAN_DIPS", "").strip().lower() != "true":
        return []
    try:
        from backend.broker.alpaca import scan_for_extreme_dips
        from backend.signals.engine import detect_macro_regime

        tickers = [
            t.strip().upper()
            for t in os.getenv("TICKER_UNIVERSE", "SPY,QQQ,GLD,TLT,XLE,IBIT").split(",")
            if t.strip()
        ]
        macro_regime = detect_macro_regime()
        return scan_for_extreme_dips(tickers, {}, macro_regime)
    except Exception as e:
        print(f"Live dip scan failed: {str(e)[:120]}")
        return []


def build_summary() -> str:
    try:
        from database.client import get_trade_stats, get_recent_trades

        stats = get_trade_stats(days=1)
        trades = get_recent_trades(days=1)
        logged_dips = _recent_logged_dips()
        live_dips = _live_dip_scan()

        seen = set()
        dips = []
        for item in logged_dips + live_dips:
            key = (item.get("ticker"), item.get("type"), item.get("dip_score"))
            if key in seen:
                continue
            seen.add(key)
            dips.append(item)

        lines = [
            "*Trading Agent Update*",
            datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            "",
            f"Trades today: {stats.get('total', 0)}",
            f"Win rate: {stats.get('win_rate', 0):.0f}%",
            f"P&L today: EUR {stats.get('total_pnl_eur', 0):+.2f}",
        ]

        if dips:
            lines.extend(["", "*Extreme dip alerts:*"])
            for d in dips[:8]:
                lines.append(
                    f"{d.get('ticker','?')} {d.get('type','dip')} "
                    f"score {d.get('dip_score', 0):.2f} | "
                    f"{d.get('pct_from_high', 0):.1f}% below 20d high | "
                    f"RSI {d.get('rsi', '?')} | macro {d.get('macro_regime', '?')}"
                )

        if trades:
            lines.extend(["", "*Recent closed trades:*"])
            for t in trades[:3]:
                pnl = t.get("net_pnl_pct", 0) or 0
                lines.append(
                    f"{t.get('ticker','?')} {t.get('side','?')} "
                    f"{pnl:+.2f}% ({t.get('exit_reason','?')})"
                )

        return "\n".join(lines)
    except Exception as e:
        return f"Trading agent update\nSummary error: {str(e)[:120]}"


if __name__ == "__main__":
    msg = build_summary()
    print(msg)
    send_message(msg)
