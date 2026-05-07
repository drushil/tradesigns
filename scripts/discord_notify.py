"""
scripts/discord_notify.py
Sends a throttled cycle summary to Discord.
Called by GitHub Actions after the signal cycle step.
"""
import os
import requests
from datetime import datetime, timezone

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
SUMMARY_EVENT = "discord_hourly_summary_sent"
SUMMARY_INTERVAL_MINUTES = int(os.getenv("DISCORD_SUMMARY_INTERVAL_MINUTES", "60") or "60")


def send_message(text: str):
    if not WEBHOOK_URL:
        print("Discord webhook not configured — skipping notification")
        return
    resp = requests.post(WEBHOOK_URL, json={"content": text}, timeout=10)
    return resp.ok


def _parse_dt(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def should_send_summary() -> bool:
    """Throttle routine Discord summaries without blocking urgent agent alerts."""
    if SUMMARY_INTERVAL_MINUTES <= 0:
        return True
    try:
        from database.client import get_logs

        now = datetime.now(timezone.utc)
        for row in get_logs(limit=500):
            if row.get("event") != SUMMARY_EVENT:
                continue
            logged_at = _parse_dt(row.get("logged_at"))
            if logged_at and (now - logged_at).total_seconds() < SUMMARY_INTERVAL_MINUTES * 60:
                print(f"Discord summary sent recently at {logged_at.isoformat()} — skipping")
                return False
    except Exception as e:
        print(f"Discord summary throttle check failed — sending anyway: {str(e)[:120]}")
    return True


def record_summary_sent():
    try:
        from database.client import log_event

        log_event("INFO", SUMMARY_EVENT, {
            "interval_minutes": SUMMARY_INTERVAL_MINUTES,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        print(f"Could not record Discord summary throttle marker: {str(e)[:120]}")


def build_summary() -> str:
    try:
        from backend.broker.alpaca import get_account, get_positions
        from database.client import get_recent_trades, get_logs, get_open_trade_records

        account   = get_account()
        positions = get_positions()
        trades    = get_recent_trades(days=1)
        open_trades = get_open_trade_records()

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
                    pnl = float(pnl)
                except Exception:
                    pnl = 0
                emoji = "🟢" if pnl >= 0 else "🔴"
                pos_parts.append(f"{emoji} {p.get('ticker','?')} ({pnl:+.1f}%)")
            lines.append("📂 Positions: " + "  ".join(pos_parts))

        # Swing exposure
        swing_records = [
            r for r in open_trades
            if r.get("promoted_to_swing") or r.get("swing_trade") or r.get("horizon") == "swing"
        ]
        if swing_records:
            position_by_ticker = {p.get("ticker"): p for p in positions}
            swing_parts = []
            for rec in swing_records[:6]:
                ticker = rec.get("ticker", "?")
                pos = position_by_ticker.get(ticker, {})
                pnl = pos.get("unrealized_plpc")
                pnl_text = ""
                if pnl is not None:
                    try:
                        pnl_text = f" {float(pnl):+.1f}%"
                    except Exception:
                        pnl_text = ""
                tag = "promoted" if rec.get("promoted_to_swing") else "swing"
                conviction = rec.get("swing_conviction")
                conv_text = ""
                if conviction:
                    try:
                        conv_text = f" c{float(conviction):.0%}"
                    except Exception:
                        conv_text = ""
                swing_parts.append(f"{ticker} {tag}{pnl_text}{conv_text}")
            lines.append("🏌️ Swings: " + "  ".join(swing_parts))

        closed_swings = [
            t for t in trades
            if t.get("promoted_to_swing") or t.get("swing_trade") or t.get("horizon") == "swing"
        ]
        if closed_swings:
            swing_pnl = sum((t.get("pnl_eur") or 0) for t in closed_swings)
            swing_wins = sum(1 for t in closed_swings if (t.get("net_pnl_pct") or 0) > 0)
            lines.append(
                f"🏌️ Closed swings today: {len(closed_swings)}"
                f"  |  W:{swing_wins} L:{len(closed_swings) - swing_wins}"
                f"  |  P&L: €{swing_pnl:+.2f}"
            )

        # Today's cycle activity
        total_trades = len(trades)
        wins         = [t for t in trades if (t.get("net_pnl_pct") or 0) > 0]
        win_rate     = len(wins) / total_trades * 100 if total_trades else 0
        pnl_today    = sum((t.get("pnl_eur") or 0) for t in trades)
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
                    f"@ ${(t.get('entry_price') or 0):.2f}  "
                    f"conviction {conviction:.2f}  "
                    f"→ {t.get('exit_reason') or 'open'}"
                    + (f"  {pnl:+.2f}%" if t.get('exit_reason') else "")
                )

        # Gated / skipped signals from this cycle
        try:
            logs = get_logs(limit=50, level="INFO")
            gated = [l for l in logs if l.get("event") == "trade_gated"]
            if gated:
                lines.append(f"🚧 Recent gated signals: {len(gated)}")
                for g in gated[:3]:
                    detail = g.get("detail") or {}
                    lines.append(
                        f"  ↳ {detail.get('ticker','?')} — {detail.get('reason','?')}"
                    )
        except Exception:
            pass

        return "\n".join(lines)

    except Exception as e:
        return f"🤖 Agent cycle complete\n⚠️ Summary error: {str(e)[:120]}"


if __name__ == "__main__":
    if should_send_summary():
        msg = build_summary()
        print(msg)
        if send_message(msg):
            record_summary_sent()
    else:
        print("Routine Discord summary skipped by hourly throttle.")
