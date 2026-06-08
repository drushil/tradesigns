"""
backend/runtime/lifecycle.py
Scheduled lifecycle entry points that run outside the main signal cycle:
  run_nightly_sweep          — post-close sweep + gate-controller (Mon–Fri)
  run_post_market_analytics  — blocked-opp / closed-trade replay (Mon–Fri)
  run_daily_eod_review       — read-only daily post-market synthesis
  run_portfolio_review       — advisory position scoring (weekly)
  run_weekly_digest          — EWA learning digest via Claude Sonnet (weekly)

These are called directly from GitHub Actions and from start_scheduler() in
backend/agent.py.  They do not call run_signal_cycle() and have no dependency
on agent-level state (_open_trades, _signal_cache, etc.).

_send_discord_alert is lazy-imported from backend.agent inside run_nightly_sweep
to break the potential circular import at module load time.
"""
from __future__ import annotations

from database.client import log_event
from backend.runtime.env import _env_bool, _env_float
from backend.broker.alpaca import get_account, get_positions
from backend.sweep.agent import compute_sweep_plan, execute_sweep
from backend.analytics.replay import (
    _replay_advisory_signals,
    _replay_blocked_opportunities,
    _replay_closed_trade_exits,
)
from backend.learning.engine import generate_weekly_insights


# ---------------------------------------------------------------------------
# Nightly sweep
# ---------------------------------------------------------------------------

def run_nightly_sweep():
    """Runs after US market close every weekday. Simulation on alpaca_paper, live on ibkr_live."""
    try:
        try:
            from backend.learning.gate_controller import run_b_shadow_promotion_controller
            run_b_shadow_promotion_controller()
        except Exception as e:
            log_event("WARN", "b_shadow_promotion_controller_error", {"error": str(e)[:160]})

        if not _env_bool("SWEEP_ENABLED", False):
            log_event("INFO", "nightly_sweep_skipped", {"reason": "disabled"})
            return

        account = get_account()
        if "error" in account:
            log_event("ERROR", "nightly_sweep_account_error", {"error": account["error"]})
            return

        fx_rate = _env_float("EURUSD_RATE", 1.08)
        positions = get_positions()
        portfolio_state = {
            "equity_eur":      round(account.get("portfolio_value", 0) / fx_rate, 2),
            "cash_eur":        round(account.get("cash", 0) / fx_rate, 2),
            "open_positions":  len(positions),
            "pending_signals": 0,
        }

        plan   = compute_sweep_plan(portfolio_state)
        result = execute_sweep(plan)

        log_event("INFO", "nightly_sweep", result)

        if result.get("mode") == "simulation" and result.get("should_sweep"):
            from backend.agent import _send_discord_alert  # lazy — avoids circular at load
            _send_discord_alert(
                f"💰 Sweep simulation: Would park "
                f"€{plan['sweepable_eur']:.0f} in {plan['sweep_ticker']}. "
                f"Est. daily yield: €{plan['est_daily_yield']:.2f}"
            )
    except Exception as e:
        log_event("ERROR", "nightly_sweep_failed", {"error": str(e)[:100]})


# ---------------------------------------------------------------------------
# Post-market analytics
# ---------------------------------------------------------------------------

def run_post_market_analytics():
    """
    Runs after US market close (21:05 UTC / 5:05 PM ET).
    Replays blocked opportunities and closed trade exits against post-event
    price action. Kept out of the signal cycle to avoid I/O overhead.
    """
    try:
        log_event("INFO", "post_market_analytics_start", {})
        _replay_advisory_signals()
        _replay_blocked_opportunities()
        _replay_closed_trade_exits()
        log_event("INFO", "post_market_analytics_complete", {})
    except Exception as e:
        log_event("ERROR", "post_market_analytics_failed", {"error": str(e)[:160]})


# ---------------------------------------------------------------------------
# Daily EOD review
# ---------------------------------------------------------------------------

def run_daily_eod_review():
    """Run read-only daily post-market synthesis and recommendations.

    Also triggers the advisory-auto sim EOD mark-to-close so any fills that
    survived to the session bell are cleanly recorded as closed_eod before
    the scoreboard is read.
    """
    # Advisory-auto sim EOD close — belt-and-suspenders in addition to the
    # inline call at the end of each simulation cycle.
    try:
        from backend.advisory_auto.simulator import run_advisory_auto_eod_close
        eod_result = run_advisory_auto_eod_close(market="US")
        log_event("INFO", "advisory_auto_eod_close_from_eod_review", eod_result)
    except Exception as e:
        log_event("WARN", "advisory_auto_eod_close_failed", {"error": str(e)[:160]})

    try:
        from backend.daily_review import run_daily_eod_review as _review
        return _review()
    except Exception as e:
        log_event("ERROR", "daily_eod_review_failed", {"error": str(e)[:160]})
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Weekly portfolio review (advisory)
# ---------------------------------------------------------------------------

def run_portfolio_review():
    """
    Advisory portfolio review — observation and recommendation only.
    Scores every open position and writes hold/trim/add/exit recommendations
    to portfolio_reviews. No trades are placed.
    Called weekly (Sunday 17:00 UTC), one hour before the weekly digest.
    Execution authority is granted only after 8-10 weeks of validated recommendations.
    """
    from backend.portfolio.advisor import run_portfolio_review as _review
    try:
        result = _review()
        if result.get("skipped"):
            log_event("INFO", "portfolio_review_skipped", result)
        else:
            log_event("LEARNING", "portfolio_review_ok", {
                "positions": result.get("position_count", 0),
                "summary":   result.get("summary", {}),
                "alerts":    len(result.get("alerts", [])),
            })
        return result
    except Exception as e:
        log_event("ERROR", "portfolio_review_error", {"error": str(e)[:200]})
        return {}


# ---------------------------------------------------------------------------
# Weekly digest
# ---------------------------------------------------------------------------

def run_weekly_digest():
    from database.client import get_recent_trades, get_daily_reviews, save_learning
    trades = get_recent_trades(days=7)
    daily_reviews = get_daily_reviews(limit=7)
    if not trades and not daily_reviews:
        return
    try:
        from backend.learning.gate_controller import run_gate_controller
        run_gate_controller(days=7, limit=500)
    except Exception as e:
        log_event("WARN", "gate_controller_error", {"error": str(e)[:160]})
    advisory_summary = {}
    try:
        from database.client import get_advisory_attribution_summary
        advisory_summary = get_advisory_attribution_summary(days=90)
    except Exception as e:
        log_event("WARN", "advisory_attribution_error", {"error": str(e)[:160]})
    insights = generate_weekly_insights(
        trades, daily_reviews=daily_reviews, advisory_summary=advisory_summary
    )
    from datetime import date
    save_learning(
        week_start      = date.today(),
        insights        = insights,
        trades_analysed = len(trades)
    )
    log_event("LEARNING", "weekly_digest", {"insights": len(insights)})
    return insights
