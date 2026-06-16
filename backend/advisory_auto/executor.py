"""
backend/advisory_auto/executor.py
Advisory-auto executor.

Grades A+, A, B are evaluated. Eligible signals are sized using the advisory
signal's own suggested_size_eur scaled by a grade multiplier:
  A+  → 100% of suggested_size_eur
  A   →  70% of suggested_size_eur
  B   →  50% of suggested_size_eur

When multiple signals pass all gates in one cycle they are processed in
grade-priority order (A+ first, then A, then B) with composite score as the
tiebreaker within the same grade. Each trade sizes independently — no
proportional scaling based on concurrent count. The hard position cap
(MAX_POSITIONS) stops new entries once reached.

Dry-run decisions and paper execution are intentionally separate. Keep
ADVISORY_AUTO_DRY_RUN=true to retain the control-group decision log; set
ADVISORY_AUTO_PAPER_EXECUTION=true to submit qualifying orders to the dedicated
Alpaca paper account at the same time.

Environment variables (all optional — safe defaults provided):
  ADVISORY_AUTO_CAPITAL_EUR         Paper capital budget (default: 20000)
  ADVISORY_AUTO_MAX_POSITIONS       Max concurrent open positions (default: 3)
  ADVISORY_AUTO_DAILY_LOSS_EUR      Stop trading if day P&L < this (default: -500)
  ADVISORY_AUTO_ALLOC_A_PLUS        % of suggested_size_eur for A+ (default: 100)
  ADVISORY_AUTO_ALLOC_A             % of suggested_size_eur for A  (default: 70)
  ADVISORY_AUTO_ALLOC_B             % of suggested_size_eur for B  (default: 50)
  ADVISORY_AUTO_MAX_SIGNAL_AGE_MIN  Max signal age to consider (default: 5)
  ADVISORY_AUTO_DRY_RUN             Keep dry-run decision logging (default: true)
  ADVISORY_AUTO_PAPER_EXECUTION     Submit paper bracket orders (default: false)
  ADVISORY_AUTO_ALLOWED_STAGES      Comma-separated alert_stage values to accept (default: trade,watch)
                                    trade = signal says price is in band now; watch = limit order left to work
  ADVISORY_AUTO_MIN_PAPER_GRADE     Min grade submitted to paper (default: B → all). Set A/A+ to gate.
                                    Dry-run eligibility tracking is unaffected.
  ADVISORY_AUTO_SUBMIT_RETRIES      Retries on transient broker errors (5xx/timeout) (default: 2)
  ADVISORY_AUTO_SUBMIT_RETRY_DELAY_S  Delay between submit retries in seconds (default: 2)
  ADVISORY_AUTO_ALPACA_API_KEY      Separate paper account key (falls back to ALPACA_API_KEY)
  ADVISORY_AUTO_ALPACA_SECRET_KEY   Separate paper account secret (falls back to ALPACA_SECRET_KEY)
"""
from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone
from typing import Optional

from database.client import (
    get_active_advisory_auto_signals,
    get_advisory_auto_eligible,
    get_advisory_auto_daily_pnl,
    get_advisory_auto_open_count,
    insert_trade,
    mark_advisory_auto_decision,
    log_event,
    update_advisory_auto_fields,
)

# ── Config ────────────────────────────────────────────────────────────────────

CAPITAL_EUR        = float(os.getenv("ADVISORY_AUTO_CAPITAL_EUR", "20000"))
MAX_POSITIONS      = int(os.getenv("ADVISORY_AUTO_MAX_POSITIONS", "3"))
DAILY_LOSS_LIMIT   = float(os.getenv("ADVISORY_AUTO_DAILY_LOSS_EUR", "-500"))
ALLOC_A_PLUS       = float(os.getenv("ADVISORY_AUTO_ALLOC_A_PLUS", "100")) / 100   # 1.00
ALLOC_A            = float(os.getenv("ADVISORY_AUTO_ALLOC_A",      "70"))  / 100   # 0.70
ALLOC_B            = float(os.getenv("ADVISORY_AUTO_ALLOC_B",      "50"))  / 100   # 0.50
MAX_SIGNAL_AGE_MIN = float(os.getenv("ADVISORY_AUTO_MAX_SIGNAL_AGE_MIN", "5"))
DRY_RUN            = os.getenv("ADVISORY_AUTO_DRY_RUN", "true").strip().lower() != "false"
PAPER_EXECUTION    = os.getenv("ADVISORY_AUTO_PAPER_EXECUTION", "false").strip().lower() in {"1", "true", "yes", "on"}
_ALLOWED_STAGES_RAW = os.getenv("ADVISORY_AUTO_ALLOWED_STAGES", "trade,watch")
ALLOWED_STAGES     = {s.strip().lower() for s in _ALLOWED_STAGES_RAW.split(",") if s.strip()}

# Minimum grade eligible for paper-order submission. Dry-run eligibility tracking
# is unaffected — this only gates which eligible signals reach Alpaca. Default 'B'
# is backwards-compatible (all grades submit). Set to 'A' / 'A+' to reduce order
# count and collect cleaner paper-vs-sim comparison data.
MIN_PAPER_GRADE    = (os.getenv("ADVISORY_AUTO_MIN_PAPER_GRADE", "B") or "B").strip().upper()

# Transient broker-error retry on order submission (e.g. Alpaca paper 5xx).
SUBMIT_MAX_RETRIES   = int(os.getenv("ADVISORY_AUTO_SUBMIT_RETRIES", "2"))
SUBMIT_RETRY_DELAY_S = float(os.getenv("ADVISORY_AUTO_SUBMIT_RETRY_DELAY_S", "2"))

# Grade ordering for priority sort (lower = higher priority)
_GRADE_PRIORITY = {"A+": 0, "A": 1, "B": 2}
_GRADE_ALLOC    = {"A+": ALLOC_A_PLUS, "A": ALLOC_A, "B": ALLOC_B}


def _grade_meets_paper_min(grade: str) -> bool:
    """True if `grade` is at or above the configured paper-execution floor."""
    threshold = _GRADE_PRIORITY.get(str(MIN_PAPER_GRADE).strip().upper(), 2)
    return _GRADE_PRIORITY.get(str(grade).strip().upper(), 9) <= threshold

_SKIP_STALE        = "skipped_stale"
_SKIP_INVALID      = "skipped_invalid_levels"
_SKIP_EXPIRED      = "skipped_expired"
_SKIP_STAGE        = "skipped_stage_not_trade"
_SKIP_OUTSIDE_BAND = "skipped_price_outside_band"
_SKIP_CHASE        = "skipped_chase"
_SKIP_ALPACA_LONG  = "skipped_existing_alpaca_exposure"
_SKIP_PENDING      = "skipped_pending_order"
_SKIP_POSITION_CAP = "skipped_position_cap"
_SKIP_DAILY_LOSS   = "skipped_daily_loss"
_SKIP_ORDER_FAILED = "paper_order_failed"

_TERMINAL_CANCEL_STATUSES = {"canceled", "cancelled", "expired", "rejected"}


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
        from alpaca.trading.enums import QueryOrderStatus
        client = _get_auto_client()
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)
        orders = client.get_orders(filter=req)
        return {o.symbol for o in orders}
    except Exception as e:
        log_event("WARN", "advisory_auto_orders_failed", {"error": str(e)[:160]})
        return set()


def _get_auto_order(order_id: str):
    """Fetch a paper order, including bracket legs when supported by the SDK."""
    from alpaca.trading.requests import GetOrderByIdRequest

    client = _get_auto_client()
    try:
        return client.get_order_by_id(order_id, GetOrderByIdRequest(nested=True))
    except TypeError:
        return client.get_order_by_id(order_id)


def _status_text(value) -> str:
    raw = str(value or "").strip().lower()
    return raw.split(".")[-1]


def _float_attr(obj, name: str, default: float = 0.0) -> float:
    try:
        return float(getattr(obj, name, None) or default)
    except Exception:
        return default


def _eurusd_rate(signal: dict = None) -> float:
    signal = signal or {}
    for value in (signal.get("fx_rate"), os.getenv("EURUSD_RATE")):
        try:
            rate = float(value or 0)
            if rate > 0:
                return rate
        except Exception:
            pass
    return 1.08


def _round_price(price: float) -> float:
    return round(float(price), 2) if float(price) >= 1 else round(float(price), 4)


def _client_order_id(signal_id: int, ticker: str) -> str:
    ticker_safe = "".join(ch for ch in str(ticker).upper() if ch.isalnum())[:10]
    return f"advauto-{signal_id}-{ticker_safe}".lower()


def _paper_order_levels(signal: dict, current_price: float, size_eur: float) -> dict:
    ticker = str(signal.get("data_symbol") or "").upper()
    entry_min = float(signal.get("entry_min") or 0)
    entry_max = float(signal.get("entry_max") or 0)
    stop_price = float(signal.get("stop_price") or 0)
    target_1 = float(signal.get("target_1") or 0)
    if not ticker or entry_min <= 0 or entry_max <= 0 or stop_price <= 0 or target_1 <= 0:
        return {"error": "invalid_order_levels"}
    if current_price <= 0:
        current_price = entry_max
    limit_price = min(max(float(current_price), entry_min), entry_max)
    if not (stop_price < limit_price < target_1):
        return {
            "error": "invalid_bracket_geometry",
            "limit_price": limit_price,
            "stop_price": stop_price,
            "target_1": target_1,
        }

    size_usd = float(size_eur) * _eurusd_rate(signal)
    qty = math.floor(size_usd / limit_price)
    if qty <= 0:
        return {
            "error": "quantity_below_one_share",
            "limit_price": limit_price,
            "size_usd": round(size_usd, 2),
        }

    return {
        "ticker": ticker,
        "qty": qty,
        "limit_price": _round_price(limit_price),
        "take_profit_price": _round_price(target_1),
        "stop_price": _round_price(stop_price),
        "size_usd": round(qty * limit_price, 2),
        "size_eur": round((qty * limit_price) / _eurusd_rate(signal), 2),
    }


def _is_transient_broker_error(exc: Exception) -> bool:
    """Retry server-side (5xx) and connection/timeout errors; never retry 4xx
    (e.g. 422 invalid order — retrying would just fail again)."""
    status = getattr(exc, "status_code", None)
    if status is not None:
        try:
            return int(status) >= 500
        except (TypeError, ValueError):
            return False
    name = exc.__class__.__name__.lower()
    return any(k in name for k in ("timeout", "connection", "transport"))


def _submit_order_with_retry(req):
    """Submit an order, retrying transient broker errors up to SUBMIT_MAX_RETRIES."""
    for attempt in range(SUBMIT_MAX_RETRIES + 1):
        try:
            return _get_auto_client().submit_order(req)
        except Exception as exc:
            if attempt < SUBMIT_MAX_RETRIES and _is_transient_broker_error(exc):
                log_event("WARN", "advisory_auto_submit_retry", {
                    "symbol": getattr(req, "symbol", None),
                    "attempt": attempt + 1,
                    "error": str(exc)[:160],
                })
                time.sleep(SUBMIT_RETRY_DELAY_S)
                continue
            raise


def _submit_paper_bracket_order(signal: dict, current_price: float, size_eur: float) -> dict:
    """Submit a long-only limit bracket order to the advisory-auto paper account."""
    levels = _paper_order_levels(signal, current_price, size_eur)
    if "error" in levels:
        return levels

    try:
        from alpaca.trading.requests import LimitOrderRequest, TakeProfitRequest, StopLossRequest
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce

        signal_id = int(signal["id"])
        req = LimitOrderRequest(
            symbol=levels["ticker"],
            qty=levels["qty"],
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=levels["limit_price"],
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=levels["take_profit_price"]),
            stop_loss=StopLossRequest(stop_price=levels["stop_price"]),
            client_order_id=_client_order_id(signal_id, levels["ticker"]),
        )
        order = _submit_order_with_retry(req)
        return {
            **levels,
            "order_id": str(order.id),
            "client_order_id": _client_order_id(signal_id, levels["ticker"]),
            "status": _status_text(getattr(order, "status", "")),
            "submitted_qty": _float_attr(order, "qty", levels["qty"]),
        }
    except Exception as exc:
        return {"error": str(exc)[:200], **levels}


def _filled_exit_leg(order) -> Optional[dict]:
    for leg in getattr(order, "legs", None) or []:
        status = _status_text(getattr(leg, "status", ""))
        filled_qty = _float_attr(leg, "filled_qty", 0.0)
        filled_price = _float_attr(leg, "filled_avg_price", 0.0)
        if status == "filled" and filled_qty > 0 and filled_price > 0:
            order_type = _status_text(getattr(leg, "order_type", ""))
            return {
                "price": filled_price,
                "qty": filled_qty,
                "exit_reason": "take_profit" if "limit" in order_type else "stop_loss",
                "order_id": str(getattr(leg, "id", "")),
            }
    return None


def _insert_closed_trade(signal: dict, exit_leg: dict, order) -> dict:
    entry_price = float(signal.get("auto_fill_price") or _float_attr(order, "filled_avg_price", 0.0))
    qty = float(signal.get("auto_fill_qty") or exit_leg.get("qty") or _float_attr(order, "filled_qty", 0.0))
    exit_price = float(exit_leg["price"])
    pnl_usd = (exit_price - entry_price) * qty
    pnl_eur = pnl_usd / _eurusd_rate(signal)
    entry_time = getattr(order, "filled_at", None) or getattr(order, "updated_at", None)
    exit_time = datetime.utcnow().isoformat() + "Z"
    return insert_trade({
        "ticker": signal.get("data_symbol"),
        "side": "BUY",
        "entry_price": round(entry_price, 4),
        "exit_price": round(exit_price, 4),
        "quantity": round(qty, 6),
        "stop_price": signal.get("stop_price"),
        "take_profit_price": signal.get("target_1"),
        "order_id": signal.get("auto_order_id"),
        "close_order_id": exit_leg.get("order_id"),
        "size_eur": round((entry_price * qty) / _eurusd_rate(signal), 2),
        "size_usd": round(entry_price * qty, 2),
        "pnl_pct": round(((exit_price - entry_price) / entry_price) * 100, 4) if entry_price else None,
        "net_pnl_pct": round(((exit_price - entry_price) / entry_price) * 100, 4) if entry_price else None,
        "pnl_eur": round(pnl_eur, 2),
        "entry_time": str(entry_time) if entry_time else None,
        "exit_time": exit_time,
        "exit_reason": exit_leg["exit_reason"],
        "composite_score": signal.get("composite_score"),
        "signals_json": signal.get("signal_json") or {},
        "trade_source": "advisory_auto",
        "advisory_signal_id": signal.get("id"),
        "horizon": "intraday",
        "strategy_family": "advisory_auto",
    })


def _reconcile_active_orders() -> dict:
    """Sync submitted/filled advisory-auto paper orders from Alpaca into Supabase."""
    active = get_active_advisory_auto_signals(limit=100)
    result = {"checked": 0, "filled": 0, "closed": 0, "terminal": 0, "errors": 0}
    for signal in active:
        result["checked"] += 1
        signal_id = signal.get("id")
        order_id = signal.get("auto_order_id")
        try:
            order = _get_auto_order(order_id)
            status = _status_text(getattr(order, "status", ""))
            if status in _TERMINAL_CANCEL_STATUSES:
                mapped = "cancelled" if status in {"canceled", "cancelled", "expired"} else "rejected"
                update_advisory_auto_fields(signal_id, {
                    "auto_status": mapped,
                    "auto_exit_reason": status,
                })
                result["terminal"] += 1
                continue

            filled_price = _float_attr(order, "filled_avg_price", 0.0)
            filled_qty = _float_attr(order, "filled_qty", 0.0)
            if status == "filled" and str(signal.get("auto_status")) == "submitted":
                update_advisory_auto_fields(signal_id, {
                    "auto_status": "filled",
                    "auto_fill_price": round(filled_price, 4) if filled_price else None,
                    "auto_fill_qty": round(filled_qty, 6) if filled_qty else None,
                })
                signal = {**signal, "auto_status": "filled",
                          "auto_fill_price": filled_price, "auto_fill_qty": filled_qty}
                result["filled"] += 1

            exit_leg = _filled_exit_leg(order)
            if status == "filled" and exit_leg:
                trade = _insert_closed_trade(signal, exit_leg, order)
                if "error" in trade:
                    result["errors"] += 1
                    log_event("WARN", "advisory_auto_trade_insert_failed", {
                        "advisory_signal_id": signal_id,
                        "error": str(trade["error"])[:160],
                    })
                    continue
                pnl_eur = trade.get("pnl_eur")
                update_advisory_auto_fields(signal_id, {
                    "auto_status": "closed",
                    "auto_pnl_eur": pnl_eur,
                    "auto_exit_reason": exit_leg["exit_reason"],
                })
                result["closed"] += 1
        except Exception as exc:
            result["errors"] += 1
            log_event("WARN", "advisory_auto_reconcile_failed", {
                "advisory_signal_id": signal_id,
                "order_id": order_id,
                "error": str(exc)[:160],
            })

    if result["checked"] or result["errors"]:
        log_event("INFO", "advisory_auto_reconcile", result)
    return result


def _signal_age_minutes(signal: dict) -> float:
    try:
        created_raw = signal.get("created_at") or ""
        created = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created).total_seconds() / 60.0
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
    """Fetch latest trade price from the advisory-auto Alpaca account."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest
        api_key = os.getenv("ADVISORY_AUTO_ALPACA_API_KEY") or os.getenv("ALPACA_API_KEY")
        secret  = os.getenv("ADVISORY_AUTO_ALPACA_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
        client  = StockHistoricalDataClient(api_key, secret)
        resp    = client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=ticker))
        return float(resp[ticker].price)
    except Exception:
        return None


def _compute_size_eur(signal: dict) -> float:
    """
    Position size = suggested_size_eur × grade_allocation_multiplier.
    Hard cap at 15% of CAPITAL_EUR regardless of grade.
    Falls back to risk-based estimate when suggested_size_eur is absent.
    """
    grade = str(signal.get("grade") or "B")
    alloc = _GRADE_ALLOC.get(grade, ALLOC_B)
    suggested = float(signal.get("suggested_size_eur") or 0)

    if suggested > 0:
        size_eur = suggested * alloc
    else:
        # Fallback: risk-based estimate (risk_eur / alloc → ~5% capital equivalent)
        risk_eur = float(signal.get("risk_eur") or CAPITAL_EUR * 0.004)
        size_eur = (risk_eur / 0.02) * alloc  # assume ~2% stop as denominator

    return round(min(size_eur, CAPITAL_EUR * 0.15), 2)


def _sort_key(signal: dict) -> tuple:
    """Grade-priority then composite score descending (negate for ascending sort)."""
    grade = str(signal.get("grade") or "B")
    priority = _GRADE_PRIORITY.get(grade, 9)
    composite = float(signal.get("composite_score") or 0)
    return (priority, -composite)


def run_advisory_auto_cycle() -> dict:
    """
    Run one advisory-auto evaluation cycle.

    Signals are evaluated in grade-priority order (A+ → A → B), composite
    score as tiebreaker. Each signal is gated independently. The first
    MAX_POSITIONS eligible signals are accepted; the rest are skipped due
    to position cap.

    Phase 2: dry-run — gates applied, decisions logged, no orders placed.
    """
    results: dict = {
        "cycle_at": datetime.utcnow().isoformat() + "Z",
        "dry_run": DRY_RUN,
        "paper_execution": PAPER_EXECUTION,
        "reconcile": _reconcile_active_orders(),
        "eligible": [],
        "submitted": [],
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
    alpaca_positions  = _get_alpaca_positions()
    alpaca_open_orders = _get_alpaca_open_orders()

    # ── Fetch + sort signals: A+ first, then A, then B; composite desc within grade ──
    raw_signals = get_advisory_auto_eligible(
        market="US", max_age_minutes=int(MAX_SIGNAL_AGE_MIN) + 1
    )
    signals = sorted(raw_signals, key=_sort_key)

    # Track tickers already accepted this cycle to avoid double-entry
    accepted_tickers: set[str] = set()
    eligible_this_cycle = 0

    for sig in signals:
        signal_id = sig["id"]
        ticker    = str(sig.get("data_symbol") or "").upper()
        grade     = str(sig.get("grade") or "B")
        skip_reason: Optional[str] = None

        try:
            # Gate 1: signal freshness
            age_min = _signal_age_minutes(sig)
            if age_min > MAX_SIGNAL_AGE_MIN:
                skip_reason = f"{_SKIP_STALE}:{age_min:.1f}min"

            # Gate 2: signal still valid
            elif not _is_valid(sig):
                skip_reason = _SKIP_EXPIRED

            # Gate 3: alert_stage must be in ALLOWED_STAGES
            elif _alert_stage(sig) not in ALLOWED_STAGES:
                skip_reason = f"{_SKIP_STAGE}:{_alert_stage(sig)}"

            # Gate 4: price levels must exist
            elif not (sig.get("entry_min") and sig.get("entry_max") and sig.get("stop_price")):
                skip_reason = _SKIP_INVALID

            # Gate 5: live price check — mode-sensitive
            #   trade: price must be inside entry band (signal says it's in band now)
            #   watch: only reject if price has blown past do_not_chase (limit order handles fill)
            else:
                stage       = _alert_stage(sig)
                entry_min_n = float(sig.get("entry_min") or 0)
                entry_max_n = float(sig.get("entry_max") or 0)
                dnc_n       = float(sig.get("do_not_chase_price") or entry_max_n * 1.05)
                current     = _get_current_price(ticker)
                if current is not None:
                    if current > dnc_n:
                        skip_reason = f"{_SKIP_CHASE}:{current:.2f}>{dnc_n:.2f}"
                    elif stage == "trade" and not (entry_min_n <= current <= entry_max_n):
                        skip_reason = (
                            f"{_SKIP_OUTSIDE_BAND}:{current:.2f} "
                            f"band=[{entry_min_n:.2f},{entry_max_n:.2f}]"
                        )

            # Gate 6: no existing Alpaca position in this ticker
            if not skip_reason and ticker in alpaca_positions:
                skip_reason = f"{_SKIP_ALPACA_LONG}:{ticker}"

            # Gate 7: no pending Alpaca order for this ticker
            if not skip_reason and ticker in alpaca_open_orders:
                skip_reason = f"{_SKIP_PENDING}:{ticker}"

            # Gate 8: deduplicate within this cycle (same ticker already accepted)
            if not skip_reason and ticker in accepted_tickers:
                skip_reason = f"{_SKIP_ALPACA_LONG}:same_ticker_this_cycle:{ticker}"

            # Gate 9: position cap — DB count + accepted this cycle
            if not skip_reason:
                total_open = open_count + eligible_this_cycle
                if total_open >= MAX_POSITIONS:
                    skip_reason = f"{_SKIP_POSITION_CAP}:{total_open}/{MAX_POSITIONS}"

            # ── Record decision ───────────────────────────────────────────────
            if skip_reason:
                mark_advisory_auto_decision(signal_id, "skipped", skip_reason)
                results["skipped"].append({
                    "signal_id": signal_id, "ticker": ticker,
                    "grade": grade, "reason": skip_reason,
                })
            else:
                mark_advisory_auto_decision(signal_id, "eligible")
                size_eur = _compute_size_eur(sig)
                accepted_tickers.add(ticker)
                eligible_this_cycle += 1
                eligible_record = {
                    "signal_id": signal_id, "ticker": ticker,
                    "grade": grade,
                    "alloc_pct": round(_GRADE_ALLOC.get(grade, ALLOC_B) * 100),
                    "composite": sig.get("composite_score"),
                    "size_eur": size_eur,
                    "dry_run": DRY_RUN,
                }
                results["eligible"].append(eligible_record)
                log_event("INFO", "advisory_auto_eligible", {
                    "signal_id": signal_id, "ticker": ticker,
                    "grade": grade,
                    "alloc_pct": round(_GRADE_ALLOC.get(grade, ALLOC_B) * 100),
                    "size_eur": size_eur,
                    "dry_run": DRY_RUN,
                    "paper_execution": PAPER_EXECUTION,
                })
                if PAPER_EXECUTION and not _grade_meets_paper_min(grade):
                    log_event("INFO", "advisory_auto_paper_grade_withheld", {
                        "signal_id": signal_id, "ticker": ticker,
                        "grade": grade, "min_paper_grade": MIN_PAPER_GRADE,
                    })
                elif PAPER_EXECUTION:
                    order = _submit_paper_bracket_order(sig, current or 0.0, size_eur)
                    if "error" in order:
                        reason = f"{_SKIP_ORDER_FAILED}:{order['error']}"
                        mark_advisory_auto_decision(signal_id, "rejected", reason)
                        results["errors"].append({
                            "signal_id": signal_id,
                            "ticker": ticker,
                            "error": reason,
                        })
                        log_event("ERROR", "advisory_auto_order_failed", {
                            "signal_id": signal_id,
                            "ticker": ticker,
                            "error": str(order["error"])[:160],
                        })
                    else:
                        mark_advisory_auto_decision(signal_id, "submitted", extra_fields={
                            "auto_order_id": order["order_id"],
                        })
                        submitted_record = {
                            **eligible_record,
                            "order_id": order["order_id"],
                            "client_order_id": order.get("client_order_id"),
                            "qty": order.get("submitted_qty") or order.get("qty"),
                            "limit_price": order.get("limit_price"),
                            "take_profit_price": order.get("take_profit_price"),
                            "stop_price": order.get("stop_price"),
                            "broker_status": order.get("status"),
                        }
                        results["submitted"].append(submitted_record)
                        log_event("TRADE", "advisory_auto_order_submitted", submitted_record)

        except Exception as e:
            results["errors"].append({
                "signal_id": signal_id, "ticker": ticker, "error": str(e)[:160],
            })
            log_event("ERROR", "advisory_auto_gate_error",
                      {"signal_id": signal_id, "ticker": ticker, "error": str(e)[:160]})

    log_event("INFO", "advisory_auto_cycle", {
        "eligible": len(results["eligible"]),
        "submitted": len(results["submitted"]),
        "skipped": len(results["skipped"]),
        "errors": len(results["errors"]),
        "dry_run": DRY_RUN,
        "paper_execution": PAPER_EXECUTION,
    })
    return results
