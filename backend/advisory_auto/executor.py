"""
backend/advisory_auto/executor.py
Advisory-auto dry-run executor (Phase 2).

Scans recent A/A+ US live advisory signals, applies all entry gates,
and marks each signal as 'eligible' or 'skipped' with a reason.
Phase 2 only — no broker orders are submitted.

Phase 3 (actual paper bracket orders) is gated behind ADVISORY_AUTO_DRY_RUN=false
AND requires a validated dry-run history and confirmed broker account config.

Environment variables (all optional — safe defaults provided):
  ADVISORY_AUTO_CAPITAL_EUR         Paper capital budget (default: 20000)
  ADVISORY_AUTO_MAX_POSITIONS       Max concurrent open positions (default: 3)
  ADVISORY_AUTO_DAILY_LOSS_EUR      Stop trading if day P&L < this (default: -500)
  ADVISORY_AUTO_RISK_PCT_A_PLUS     Risk % of capital per A+ signal (default: 0.40)
  ADVISORY_AUTO_RISK_PCT_A          Risk % of capital per A signal (default: 0.25)
  ADVISORY_AUTO_MAX_SIGNAL_AGE_MIN  Max signal age to consider (default: 5)
  ADVISORY_AUTO_DRY_RUN             Set false to enable live orders (default: true)
  ADVISORY_AUTO_ALPACA_API_KEY      Separate paper account key (falls back to ALPACA_API_KEY)
  ADVISORY_AUTO_ALPACA_SECRET_KEY   Separate paper account secret (falls back to ALPACA_SECRET_KEY)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from database.client import (
    get_advisory_auto_eligible,
    get_advisory_auto_daily_pnl,
    get_advisory_auto_open_count,
    mark_advisory_auto_decision,
    log_event,
)

# ── Config ────────────────────────────────────────────────────────────────────

CAPITAL_EUR         = float(os.getenv("ADVISORY_AUTO_CAPITAL_EUR", "20000"))
MAX_POSITIONS       = int(os.getenv("ADVISORY_AUTO_MAX_POSITIONS", "3"))
DAILY_LOSS_LIMIT    = float(os.getenv("ADVISORY_AUTO_DAILY_LOSS_EUR", "-500"))
RISK_PCT_A_PLUS     = float(os.getenv("ADVISORY_AUTO_RISK_PCT_A_PLUS", "0.40")) / 100
RISK_PCT_A          = float(os.getenv("ADVISORY_AUTO_RISK_PCT_A", "0.25")) / 100
MAX_SIGNAL_AGE_MIN  = float(os.getenv("ADVISORY_AUTO_MAX_SIGNAL_AGE_MIN", "5"))
DRY_RUN             = os.getenv("ADVISORY_AUTO_DRY_RUN", "true").strip().lower() != "false"

_SKIP_STALE          = "skipped_stale"
_SKIP_INVALID        = "skipped_invalid_levels"
_SKIP_EXPIRED        = "skipped_expired"
_SKIP_STAGE          = "skipped_stage_not_trade"
_SKIP_OUTSIDE_BAND   = "skipped_price_outside_band"
_SKIP_CHASE          = "skipped_chase"
_SKIP_ALPACA_LONG    = "skipped_existing_alpaca_exposure"
_SKIP_PENDING        = "skipped_pending_order"
_SKIP_POSITION_CAP   = "skipped_position_cap"
_SKIP_DAILY_LOSS     = "skipped_daily_loss"


def _get_auto_client():
    """Alpaca trading client for advisory-auto — uses separate keys if configured."""
    from alpaca.trading.client import TradingClient
    api_key = os.getenv("ADVISORY_AUTO_ALPACA_API_KEY") or os.getenv("ALPACA_API_KEY")
    secret  = os.getenv("ADVISORY_AUTO_ALPACA_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
    return TradingClient(api_key, secret, paper=True)


def _get_alpaca_positions() -> dict[str, dict]:
    """Return {ticker: position_dict} from the advisory-auto Alpaca account."""
    try:
        client = _get_auto_client()
        positions = client.get_all_positions()
        return {p.symbol: {"qty": float(p.qty), "side": p.side} for p in positions}
    except Exception as e:
        log_event("WARN", "advisory_auto_positions_failed", {"error": str(e)[:160]})
        return {}


def _get_alpaca_open_orders() -> set[str]:
    """Return set of tickers with open orders in the advisory-auto account."""
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import OrderStatus
        client = _get_auto_client()
        req = GetOrdersRequest(status=OrderStatus.OPEN, limit=100)
        orders = client.get_orders(filter=req)
        return {o.symbol for o in orders}
    except Exception as e:
        log_event("WARN", "advisory_auto_orders_failed", {"error": str(e)[:160]})
        return set()


def _signal_age_minutes(signal: dict) -> float:
    try:
        created_raw = signal.get("created_at") or ""
        created = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (now - created).total_seconds() / 60.0
    except Exception:
        return 999.0


def _is_valid(signal: dict) -> bool:
    valid_until = signal.get("valid_until")
    if not valid_until:
        return True
    try:
        expiry = datetime.fromisoformat(str(valid_until).replace("Z", "+00:00"))
        return datetime.now(timezone.utc) < expiry
    except Exception:
        return True


def _alert_stage(signal: dict) -> str:
    sj = signal.get("signal_json") or {}
    if isinstance(sj, dict):
        return str(sj.get("alert_stage") or "trade")
    return "trade"


def _get_current_price(ticker: str) -> Optional[float]:
    """
    Fetch latest trade price from the advisory-auto Alpaca account.
    Returns None on failure — caller should treat as cannot verify.
    """
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest
        api_key = os.getenv("ADVISORY_AUTO_ALPACA_API_KEY") or os.getenv("ALPACA_API_KEY")
        secret  = os.getenv("ADVISORY_AUTO_ALPACA_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
        client  = StockHistoricalDataClient(api_key, secret)
        req     = StockLatestTradeRequest(symbol_or_symbols=ticker)
        resp    = client.get_stock_latest_trade(req)
        return float(resp[ticker].price)
    except Exception:
        return None


def _compute_size_eur(signal: dict) -> float:
    grade = str(signal.get("grade") or "A")
    risk_pct = RISK_PCT_A_PLUS if grade == "A+" else RISK_PCT_A
    stop_native = float(signal.get("stop_price") or 0)
    entry_max_native = float(signal.get("entry_max") or 0)
    fx = float(signal.get("fx_rate") or 1.0)

    if stop_native and entry_max_native and entry_max_native > stop_native:
        risk_per_share_native = entry_max_native - stop_native
        risk_eur = CAPITAL_EUR * risk_pct
        qty = risk_eur / (risk_per_share_native / fx)
        size_eur = qty * (entry_max_native / fx)
    else:
        size_eur = CAPITAL_EUR * risk_pct * 20  # rough fallback: 20× risk = ~5% pos
    return round(min(size_eur, CAPITAL_EUR * 0.15), 2)  # hard cap at 15% of capital


def run_advisory_auto_cycle() -> dict:
    """
    Run one advisory-auto evaluation cycle.
    Phase 2: dry-run — gates are applied, decisions logged, no orders placed.
    Returns a summary dict of this cycle's decisions.
    """
    results: dict = {
        "cycle_at": datetime.utcnow().isoformat() + "Z",
        "dry_run": DRY_RUN,
        "eligible": [],
        "skipped": [],
        "errors": [],
    }

    # ── Session-level gates ───────────────────────────────────────────────────
    daily_pnl = get_advisory_auto_daily_pnl()
    if daily_pnl <= DAILY_LOSS_LIMIT:
        log_event("WARN", "advisory_auto_daily_loss_halt",
                  {"daily_pnl_eur": daily_pnl, "limit": DAILY_LOSS_LIMIT})
        return {**results, "halted": True, "halt_reason": _SKIP_DAILY_LOSS}

    open_count = get_advisory_auto_open_count()
    if open_count >= MAX_POSITIONS:
        log_event("INFO", "advisory_auto_position_cap",
                  {"open_count": open_count, "max": MAX_POSITIONS})
        return {**results, "halted": True, "halt_reason": _SKIP_POSITION_CAP}

    # ── Fetch live Alpaca state once ──────────────────────────────────────────
    alpaca_positions = _get_alpaca_positions()
    alpaca_open_orders = _get_alpaca_open_orders()

    # ── Evaluate each eligible signal ────────────────────────────────────────
    signals = get_advisory_auto_eligible(market="US", max_age_minutes=int(MAX_SIGNAL_AGE_MIN) + 1)

    for sig in signals:
        signal_id = sig["id"]
        ticker = str(sig.get("data_symbol") or "").upper()
        skip_reason: Optional[str] = None

        try:
            # Gate 1: signal freshness
            age_min = _signal_age_minutes(sig)
            if age_min > MAX_SIGNAL_AGE_MIN:
                skip_reason = f"{_SKIP_STALE}:{age_min:.1f}min"

            # Gate 2: signal still valid
            elif not _is_valid(sig):
                skip_reason = _SKIP_EXPIRED

            # Gate 3: alert_stage must be 'trade'
            elif _alert_stage(sig) != "trade":
                skip_reason = f"{_SKIP_STAGE}:{_alert_stage(sig)}"

            # Gate 4: price levels must exist
            elif not (sig.get("entry_min") and sig.get("entry_max") and sig.get("stop_price")):
                skip_reason = _SKIP_INVALID

            # Gate 5: live price within entry band
            else:
                entry_min_native = float(sig.get("entry_min") or 0)
                entry_max_native = float(sig.get("entry_max") or 0)
                dnc_native = float(sig.get("do_not_chase_price") or entry_max_native * 1.05)
                current_price = _get_current_price(ticker)

                if current_price is not None:
                    if current_price > dnc_native:
                        skip_reason = f"{_SKIP_CHASE}:{current_price:.2f}>{dnc_native:.2f}"
                    elif not (entry_min_native <= current_price <= entry_max_native):
                        skip_reason = (
                            f"{_SKIP_OUTSIDE_BAND}:{current_price:.2f} "
                            f"band=[{entry_min_native:.2f},{entry_max_native:.2f}]"
                        )

            # Gate 6: no existing Alpaca position in this ticker
            if not skip_reason and ticker in alpaca_positions:
                skip_reason = f"{_SKIP_ALPACA_LONG}:{ticker}"

            # Gate 7: no pending Alpaca order for this ticker
            if not skip_reason and ticker in alpaca_open_orders:
                skip_reason = f"{_SKIP_PENDING}:{ticker}"

            # Gate 8: position cap (re-check with fresh DB count)
            if not skip_reason:
                fresh_count = get_advisory_auto_open_count()
                if fresh_count >= MAX_POSITIONS:
                    skip_reason = f"{_SKIP_POSITION_CAP}:{fresh_count}/{MAX_POSITIONS}"

            # ── Record decision ───────────────────────────────────────────────
            if skip_reason:
                mark_advisory_auto_decision(signal_id, "skipped", skip_reason)
                results["skipped"].append({
                    "signal_id": signal_id, "ticker": ticker,
                    "grade": sig.get("grade"), "reason": skip_reason,
                })
            else:
                mark_advisory_auto_decision(signal_id, "eligible")
                size_eur = _compute_size_eur(sig)
                results["eligible"].append({
                    "signal_id": signal_id, "ticker": ticker,
                    "grade": sig.get("grade"),
                    "composite": sig.get("composite_score"),
                    "size_eur": size_eur,
                    "dry_run": DRY_RUN,
                })
                log_event("INFO", "advisory_auto_eligible", {
                    "signal_id": signal_id, "ticker": ticker,
                    "grade": sig.get("grade"),
                    "size_eur": size_eur,
                    "dry_run": DRY_RUN,
                })

        except Exception as e:
            results["errors"].append({"signal_id": signal_id, "ticker": ticker, "error": str(e)[:160]})
            log_event("ERROR", "advisory_auto_gate_error",
                      {"signal_id": signal_id, "ticker": ticker, "error": str(e)[:160]})

    log_event("INFO", "advisory_auto_cycle", {
        "eligible": len(results["eligible"]),
        "skipped": len(results["skipped"]),
        "errors": len(results["errors"]),
        "dry_run": DRY_RUN,
    })
    return results
