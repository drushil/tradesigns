"""
scripts/telegram_notify.py
Sends a daily summary to Telegram after each agent cycle.
Called by GitHub Actions after the agent run.
"""
import os
import requests
from datetime import datetime

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")


def send_message(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram not configured — skipping notification")
        return
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id":    CHAT_ID,
        "text":       text,
        "parse_mode": "Markdown",
    }, timeout=10)
    return resp.ok


def build_summary() -> str:
    try:
        from database.client import get_account as _a
        from backend.broker.alpaca import get_account, get_positions
        from database.client import get_trade_stats, get_recent_trades

        account  = get_account()
        stats    = get_trade_stats(days=1)
        trades   = get_recent_trades(days=1)

        equity   = account.get("portfolio_value", 0)
        cash     = account.get("cash", 0)
        start    = float(os.getenv("STARTING_CAPITAL_EUR", "100"))
        cum_pnl  = (equity - start) / start * 100

        lines = [
            f"🤖 *Trading Agent — Cycle Update*",
            f"🕐 {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
            f"",
            f"💰 Portfolio: *€{equity:.2f}* ({cum_pnl:+.2f}% all-time)",
            f"💵 Cash: €{cash:.2f}",
            f"📊 Trades today: {stats.get('total', 0)}",
            f"✅ Win rate: {stats.get('win_rate', 0):.0f}%",
            f"💹 P&L today: €{stats.get('total_pnl_eur', 0):+.2f}",
        ]

        if trades:
            lines.append("")
            lines.append("*Recent trades:*")
            for t in trades[:3]:
                pnl   = t.get("net_pnl_pct", 0) or 0
                emoji = "🟢" if pnl > 0 else "🔴"
                lines.append(
                    f"{emoji} {t.get('ticker','?')} {t.get('side','?')} "
                    f"{pnl:+.2f}% ({t.get('exit_reason','?')})"
                )

        return "\n".join(lines)

    except Exception as e:
        return f"🤖 Agent cycle complete\n⚠️ Summary error: {str(e)[:100]}"


if __name__ == "__main__":
    msg = build_summary()
    print(msg)
    send_message(msg)
