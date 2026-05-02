"""
backend/sweep/agent.py
Cash sweep agent with BROKER_ENV gating.
Simulation (alpaca_paper): logs plans to Supabase, never places orders.
Live (ibkr_live): executes via IBKR broker module.
"""
import os
from datetime import datetime
from typing import Optional


def get_broker_env() -> str:
    return os.getenv("BROKER_ENV", "alpaca_paper")


def is_live() -> bool:
    """Returns True ONLY when BROKER_ENV is exactly 'ibkr_live'. Typos fail safe."""
    return get_broker_env() == "ibkr_live"


def compute_sweep_plan(portfolio_state: dict) -> dict:
    """Pure calculation — never executes anything, no side effects."""
    capital_eur = portfolio_state["equity_eur"]
    reserve_pct = float(os.getenv("SWEEP_RESERVE_PCT", "25")) / 100
    reserve_eur = capital_eur * reserve_pct
    unallocated = portfolio_state["cash_eur"]
    min_sweep   = float(os.getenv("SWEEP_MIN_EUR", "500"))

    sweepable = max(0.0, unallocated - reserve_eur)

    should_sweep = (
        sweepable >= min_sweep
        and portfolio_state["open_positions"] == 0
        and portfolio_state["pending_signals"] == 0
    )

    # SGOV ~3.5% annual / XEON ~3.2% annual — daily rate
    annual_yield_pct = 3.5 if not is_live() else 3.2
    daily_yield_eur  = sweepable * (annual_yield_pct / 100 / 252)

    if sweepable < min_sweep:
        reason = "below_min"
    elif portfolio_state["open_positions"] > 0:
        reason = "positions_open"
    else:
        reason = "eligible"

    return {
        "should_sweep":     should_sweep,
        "sweepable_eur":    round(sweepable, 2),
        "reserve_eur":      round(reserve_eur, 2),
        "sweep_ticker":     os.getenv("SWEEP_TICKER", "SGOV"),
        "est_daily_yield":  round(daily_yield_eur, 4),
        "est_annual_yield": round(sweepable * annual_yield_pct / 100, 2),
        "broker_env":       get_broker_env(),
        "reason":           reason,
    }


def execute_sweep(sweep_plan: dict) -> dict:
    """Execute or simulate the sweep. Never places orders unless BROKER_ENV=ibkr_live exactly."""
    from database.client import log_event

    result = {**sweep_plan, "executed_at": datetime.utcnow().isoformat()}

    if not sweep_plan["should_sweep"]:
        result["executed"]    = False
        result["skip_reason"] = sweep_plan["reason"]
        _log_sweep(result)
        return result

    if not is_live():
        # SIMULATION — hard guard: this branch must never place real orders
        assert not is_live(), "Simulation safety guard breached"
        result["executed"] = False
        result["mode"]     = "simulation"
        result["sim_note"] = (
            f"Would buy {sweep_plan['sweep_ticker']} "
            f"worth €{sweep_plan['sweepable_eur']:.2f}. "
            f"Est. daily yield: €{sweep_plan['est_daily_yield']:.4f}"
        )
        _log_sweep(result)
        log_event("INFO", "sweep_simulated", result)
        return result

    # LIVE — execute via IBKR (activates when backend/broker/ibkr.py exists)
    try:
        from backend.broker.ibkr import submit_order
        order = submit_order(
            ticker     = sweep_plan["sweep_ticker"],
            side       = "buy",
            amount_eur = sweep_plan["sweepable_eur"],
        )
        result["executed"]  = True
        result["mode"]      = "live"
        result["order_id"]  = order.get("id")
        _log_sweep(result)
        log_event("INFO", "sweep_executed", result)
        return result
    except ImportError:
        result["executed"] = False
        result["mode"]     = "ibkr_not_built"
        result["error"]    = "backend/broker/ibkr.py not yet built"
        _log_sweep(result)
        return result
    except Exception as e:
        result["executed"] = False
        result["error"]    = str(e)[:100]
        _log_sweep(result)
        return result


def recall_sweep(reason: str) -> dict:
    """Check if a sweep position exists and should be recalled before trading."""
    if not is_live():
        last = _get_last_sweep()
        if last and last.get("executed") is False and last.get("mode") == "simulation":
            try:
                from database.client import log_event
                log_event("INFO", "recall_simulated", {
                    "reason": reason,
                    "sim_note": f"Would sell {last.get('sweep_ticker', 'SGOV')} position",
                })
            except Exception:
                pass
            return {"recalled": False, "mode": "simulation", "reason": reason}
        return {"recalled": False, "mode": "simulation", "reason": "no_position"}

    # LIVE — sell via IBKR
    try:
        from backend.broker.ibkr import submit_order, get_position
        pos = get_position(os.getenv("SWEEP_TICKER", "XEON"))
        if pos and pos.get("qty", 0) > 0:
            order = submit_order(
                ticker = os.getenv("SWEEP_TICKER", "XEON"),
                side   = "sell",
                qty    = pos["qty"],
            )
            return {"recalled": True, "mode": "live",
                    "order_id": order["id"], "reason": reason}
        return {"recalled": False, "mode": "live", "reason": "no_position"}
    except Exception as e:
        return {"recalled": False, "error": str(e)[:100]}


def has_active_sweep() -> bool:
    """Return True if the last logged sweep was an eligible simulation (i.e. cash was parked)."""
    last = _get_last_sweep()
    return bool(last and last.get("mode") == "simulation" and last.get("should_sweep"))


def _get_last_sweep() -> Optional[dict]:
    try:
        from database.client import get_client
        db = get_client()
        result = (db.table("cash_sweeps")
                  .select("*")
                  .order("executed_at", desc=True)
                  .limit(1)
                  .execute())
        return result.data[0] if result.data else None
    except Exception:
        return None


def _log_sweep(sweep_result: dict):
    try:
        from database.client import get_client
        db = get_client(write=True)
        db.table("cash_sweeps").insert({
            "broker_env":       sweep_result.get("broker_env"),
            "sweep_ticker":     sweep_result.get("sweep_ticker"),
            "sweepable_eur":    sweep_result.get("sweepable_eur"),
            "reserve_eur":      sweep_result.get("reserve_eur"),
            "est_daily_yield":  sweep_result.get("est_daily_yield"),
            "est_annual_yield": sweep_result.get("est_annual_yield"),
            "executed":         sweep_result.get("executed", False),
            "mode":             sweep_result.get("mode", "simulation"),
            "skip_reason":      sweep_result.get("skip_reason"),
            "sim_note":         sweep_result.get("sim_note"),
            "error":            sweep_result.get("error"),
            "executed_at":      sweep_result.get("executed_at"),
        }).execute()
    except Exception as e:
        print(f"[SWEEP_LOG_FAILED] {e}")
