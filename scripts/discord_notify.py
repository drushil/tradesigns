"""
scripts/discord_notify.py
Sends a cycle summary to Discord after each agent run.
Called by GitHub Actions after the signal cycle step.
"""
import os
import requests
from datetime import datetime, timezone

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")


def send_message(text: str):
    if not WEBHOOK_URL:
        print("Discord webhook not configured — skipping notification")
        return
    resp = requests.post(WEBHOOK_URL, json={"content": text}, timeout=10)
    return resp.ok


def build_summary() -> str:
    try:
        from backend.broker.alpaca import get_account, get_positions
        from database.client import get_trade_stats, get_recent_trades, get_logs

        account   = get_account()
        positions = get_positions()
        stats     = get_trade_stats(days=1)
        trades    = get_recent_trades(days=1)

        equity    = account.get("portfolio_value", 0)
        cash      = account.get("cash", 0)
        fx_rate   = float(os.getenv("EURUSD_RATE", "1.08"))
        start_eur = float(os.getenv("STARTING_CAPITAL_EUR", "100"))
        max_eur   = float(os.getenv("MAX_CAPITAL_DEPLOYED_EUR", "0") or 0)
        equity_eur = equity / fx_rate
        cash_eur   = cash / fx_rate
        cum_pnl   = (equity_eur - start_eur) / start_eur * 100 if start_eur else 0
        now       = datetime.now(timezone.utc).strftime("%H:%M UTC")

        lines = [
            f"🤖 **Cycle Update — {now}**",
            f"💰 Portfolio: **€{equity_eur:.2f}** ({cum_pnl:+.2f}% all-time)",
            f"💵 Cash: €{cash_eur:.2f}  |  Open positions: {len(positions)}",
        ]

        # Open positions
        if positions:
            pos_parts = []
            for p in positions:
                qty = p.get("qty", 0)
                pnl = p.get("unrealized_plpc", 0) or 0
                try:
                    pnl = float(pnl) * 100
                except Exception:
                    pnl = 0
                emoji = "🟢" if pnl >= 0 else "🔴"
                pos_parts.append(f"{emoji} {p.get('ticker','?')} ({pnl:+.1f}%)")
            lines.append("📂 Positions: " + "  ".join(pos_parts))

        # Today's cycle activity
        total_trades = stats.get("total", 0)
        win_rate     = stats.get("win_rate", 0)
        pnl_today    = stats.get("total_pnl_eur", 0) or 0
        lines.append(
            f"📊 Today: {total_trades} trade{'s' if total_trades != 1 else ''}"
            + (f"  |  Win rate: {win_rate:.0f}%  |  P&L: €{pnl_today:+.2f}" if total_trades else "")
        )

        # Recent trade decisions
        if trades:
            lines.append("**Recent decisions:**")
            for t in trades[:5]:
                pnl   = t.get("net_pnl_pct") or 0
                emoji = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
                conviction = t.get("llm_conviction") or t.get("conviction") or 0
                try:
                    conviction = float(conviction)
                except Exception:
                    conviction = 0
                lines.append(
                    f"{emoji} **{t.get('ticker','?')}** {str(t.get('side','?')).upper()} "
                    f"@ €{(t.get('entry_price') or 0):.2f}  "
                    f"conviction {conviction:.2f}  "
                    f"→ {t.get('exit_reason') or 'open'}"
                    + (f"  {pnl:+.2f}%" if t.get('exit_reason') else "")
                )

        # Gated / skipped signals from this cycle
        try:
            logs = get_logs(limit=50, level="INFO")
            gated = [l for l in logs if l.get("event_type") == "trade_gated"]
            if gated:
                lines.append(f"🚧 Gated this cycle: {len(gated)}")
                for g in gated[:3]:
                    detail = g.get("details") or {}
                    lines.append(
                        f"  ↳ {detail.get('ticker','?')} — {detail.get('reason','?')}"
                    )
        except Exception:
            pass

        return "\n".join(lines)

    except Exception as e:
        return f"🤖 Agent cycle complete\n⚠️ Summary error: {str(e)[:120]}"


if __name__ == "__main__":
    msg = build_summary()
    print(msg)
    send_message(msg)
