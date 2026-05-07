"""
backend/agent.py
Main agent loop. Runs on a schedule, ties together:
signals → risk gate → EV check → LLM decision → execution → learning → logging
"""
import os
import time
import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from dotenv import load_dotenv

try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
except Exception:
    try:
        import pytz
        NY_TZ = pytz.timezone("America/New_York")
    except Exception:
        NY_TZ = None

load_dotenv()

from config.risk_profiles        import get_profile
from backend.signals.engine      import (compute_all_signals, compute_swing_score,
                                          detect_regime, detect_macro_regime,
                                          compute_atr, latest_macro_headlines,
                                          scan_for_macro_shock,
                                          detect_momentum_swing)
from backend.broker.alpaca       import (get_account, get_positions, submit_market_order,
                                          close_position, pre_trade_gate, compute_position_size,
                                          scan_for_extreme_dips, get_order_by_id,
                                          cancel_order_by_id, submit_stop_order)
from backend.learning.engine     import (RegimeAwareWeightEngine, attribute_signals,
                                          compute_expected_value, get_effective_profile,
                                          generate_weekly_insights, llm_signal_decision,
                                          build_weight_engine_from_trades)
from database.client             import (insert_trade, insert_signal, get_recent_trades,
                                          save_signal_weights, get_latest_weights,
                                          save_snapshot, save_learning, log_event, get_logs,
                                          save_open_trade, get_open_trade_records,
                                          close_open_trade_record)
from backend.sweep.agent         import (compute_sweep_plan, execute_sweep,
                                          recall_sweep, has_active_sweep)
from backend.dividends.scanner   import (scan_dividend_calendar, log_dividend_opportunity)

def _env_value(key: str, default: str) -> str:
    value = os.getenv(key)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env_value(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env_value(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool = True) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() not in {"false", "0", "no"}


def _eurusd_rate() -> float:
    return _env_float("EURUSD_RATE", 1.08)


def _get_cached_signals(ticker: str, weights: dict, regime_state) -> dict:
    """Return signals from cache if fresh, otherwise fetch and cache."""
    now = datetime.now(timezone.utc)
    cached = _signal_cache.get(ticker)
    if cached is not None:
        age = (now - cached[0]).total_seconds()
        if age < _SIGNAL_CACHE_TTL_SECONDS:
            return cached[1]
    # Evict all expired entries while we're here (keeps dict bounded to TICKERS set)
    expired = [k for k, v in _signal_cache.items()
               if (now - v[0]).total_seconds() >= _SIGNAL_CACHE_TTL_SECONDS]
    for k in expired:
        _signal_cache.pop(k, None)
    result = compute_all_signals(ticker, weights, regime_state=regime_state)
    _signal_cache[ticker] = (now, result)
    return result


def _eur_to_usd(amount_eur: float) -> float:
    return float(amount_eur or 0) * _eurusd_rate()


TICKERS  = [t.strip().upper() for t in _env_value("TICKER_UNIVERSE", "SPY,QQQ,GLD").split(",") if t.strip()]
SWING_TICKERS = [t.strip().upper() for t in _env_value("SWING_TICKERS", "").split(",") if t.strip()]
PROFILE  = get_profile(_env_value("RISK_PROFILE", "moderate"))
HORIZON  = _env_value("INVESTMENT_HORIZON", "short")
LLM_HOUR_LIMIT = _env_int("LLM_CALLS_PER_HOUR_LIMIT", 20)
IS_PAPER_TRADING = _env_value("ALPACA_PAPER", "true").lower() != "false"

# Global learning engine (persists in memory between cycles)
_learning_engine: Optional[RegimeAwareWeightEngine] = None
_llm_calls_this_hour = 0
_llm_hour_reset      = datetime.utcnow()
_open_trades         = {}   # {ticker: {entry_price, entry_time, stop_price, hold_minutes, ...}}
_swing_trades        = {}   # {ticker: {entry_price, entry_time, hold_days, ...}}
_last_shock_refresh  = None
_day_trade_log: list = []   # [(date, ticker)] same-day round trips for PDT tracking
_last_shock_result   = {
    "shock_detected": False,
    "classification": "NORMAL",
    "affected_sectors": [],
    "direction": "mixed",
    "reason": "not_scanned",
}
# Per-cycle signal cache — keyed by ticker, expires after 8 min (one cycle apart at 10-min cadence)
_signal_cache: dict[str, tuple[datetime, dict]] = {}
_SIGNAL_CACHE_TTL_SECONDS = 480

def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + (occurrence - 1) * 7)


def _to_new_york_time(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if NY_TZ is not None:
        return now.astimezone(NY_TZ)

    utc_now = now.astimezone(timezone.utc)
    year = utc_now.year
    dst_start = datetime.combine(
        _nth_weekday(year, 3, 6, 2), datetime.min.time(), timezone.utc
    ).replace(hour=7)
    dst_end = datetime.combine(
        _nth_weekday(year, 11, 6, 1), datetime.min.time(), timezone.utc
    ).replace(hour=6)
    offset = -4 if dst_start <= utc_now < dst_end else -5
    return utc_now.astimezone(timezone(timedelta(hours=offset)))


def is_regular_us_market_hours(now: datetime = None) -> bool:
    """Return True during regular US equity hours: Mon-Fri, 09:30-16:00 New York time."""
    now = now or datetime.now(timezone.utc)
    ny_now = _to_new_york_time(now)
    if ny_now.weekday() >= 5:
        return False
    market_open = ny_now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = ny_now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= ny_now < market_close


def _run_signal_cycle_if_market_open():
    if is_regular_us_market_hours():
        run_signal_cycle()


def _allows_intraday() -> bool:
    return HORIZON in {"short", "intraday", "both"}


def _allows_swing() -> bool:
    return HORIZON in {"mid", "swing", "both"}


def _open_position_tickers(portfolio_state: dict) -> set[str]:
    return {str(p.get("ticker", "")).upper() for p in portfolio_state.get("positions", [])}


def _should_run_swing_recheck(now: datetime = None) -> bool:
    """Run swing re-evaluation once in the configured New York market-open window."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ny_now = _to_new_york_time(now)
    hour = _env_int("SWING_REEVAL_NY_HOUR", 9)
    minute = _env_int("SWING_REEVAL_NY_MINUTE", 35)
    window = _env_int("SWING_REEVAL_WINDOW_MINUTES", 5)
    current = ny_now.hour * 60 + ny_now.minute
    target = hour * 60 + minute
    return ny_now.weekday() < 5 and 0 <= current - target < window


def _is_eod_intraday_cleanup_window(now: datetime) -> bool:
    """Return True during the 30-min window before the regular US market close."""
    if not _env_bool("EOD_INTRADAY_CLEANUP_ENABLED", True):
        return False
    if not is_regular_us_market_hours(now):
        return False
    ny_now = _to_new_york_time(now)
    close = _env_int("EOD_CLEANUP_NY_CLOSE_HOUR", 16) * 60 + _env_int("EOD_CLEANUP_NY_CLOSE_MINUTE", 0)
    current = ny_now.hour * 60 + ny_now.minute
    return close - _env_int("EOD_CLEANUP_BUFFER_MINUTES", 30) <= current < close


def _init_learning_engine() -> RegimeAwareWeightEngine:
    """Load latest weights from DB or use profile priors."""
    saved = get_latest_weights("global")
    trades = get_recent_trades(days=_env_int("LEARNING_LOOKBACK_DAYS", 120))
    replayable = [t for t in trades if t.get("signals_json") and t.get("net_pnl_pct") is not None]
    if replayable:
        return build_weight_engine_from_trades(PROFILE["signal_weights"], replayable)
    priors = saved if saved else PROFILE["signal_weights"]
    return RegimeAwareWeightEngine(priors)


def _get_portfolio_state() -> dict:
    account   = get_account()
    if "error" in account:
        return {"broker_error": account["error"], "equity": 0, "cash": 0, "positions": []}
    positions = get_positions()
    equity    = account.get("portfolio_value", 100.0)
    cash      = account.get("cash", 100.0)

    # VIX
    try:
        import yfinance as yf
        vix_df = yf.download("^VIX", period="1d", interval="1h",
                             progress=False, auto_adjust=True)
        vix = float(vix_df["Close"].iloc[-1].item()) if not vix_df.empty else 20.0
    except Exception:
        vix = 20.0

    # Drawdown always measured against STARTING_CAPITAL_EUR converted to USD.
    # This ensures the circuit breaker fires at the correct EUR loss amount
    # regardless of the Alpaca paper account's $100k default.
    start_eur = float(os.getenv("STARTING_CAPITAL_EUR", "3000"))
    fx_rate   = float(os.getenv("EURUSD_RATE", "1.08") or "1.08")
    start_usd = start_eur * fx_rate
    drawdown  = max(0.0, (start_usd - equity) / start_usd * 100)

    return {
        "equity":       round(equity, 2),
        "cash":         round(cash, 2),
        "cash_pct":     round(cash / equity * 100, 1) if equity > 0 else 100.0,
        "positions":    positions,
        "vix":          round(vix, 1),
        "drawdown_today": round(drawdown, 3),
        "trades_today": _count_trades_today(),
        "consecutive_losses": _count_consecutive("loss"),
        "consecutive_wins":   _count_consecutive("win"),
    }


def _count_trades_today() -> int:
    trades = get_recent_trades(days=1)
    today  = datetime.utcnow().date()
    closed_count = sum(1 for t in trades
                       if t.get("created_at", "")[:10] == str(today))
    try:
        trade_logs = get_logs(level="TRADE", limit=200)
        submitted_count = sum(
            1 for l in trade_logs
            if l.get("event") == "order_submitted"
            and (l.get("logged_at") or "")[:10] == str(today)
        )
        return max(closed_count, submitted_count)
    except Exception:
        return closed_count


def _count_consecutive(outcome: str) -> int:
    trades = get_recent_trades(days=7)
    count  = 0
    for t in trades:
        pnl = t.get("net_pnl_pct", 0) or 0
        is_win = pnl > 0
        if outcome == "win" and is_win:
            count += 1
        elif outcome == "loss" and not is_win:
            count += 1
        else:
            break
    return count


def _can_call_llm() -> bool:
    global _llm_calls_this_hour, _llm_hour_reset
    now = datetime.utcnow()
    if (now - _llm_hour_reset).seconds >= 3600:
        _llm_calls_this_hour = 0
        _llm_hour_reset      = now
    return _llm_calls_this_hour < LLM_HOUR_LIMIT


def _record_llm_call():
    global _llm_calls_this_hour
    _llm_calls_this_hour += 1


def _send_discord_alert(text: str) -> bool:
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not webhook:
        return False
    try:
        import requests
        resp = requests.post(webhook, json={"content": text}, timeout=10)
        return resp.ok
    except Exception:
        return False


def _refresh_macro_shock_if_needed() -> dict:
    global _last_shock_refresh, _last_shock_result
    now = datetime.utcnow()
    if _last_shock_refresh and (now - _last_shock_refresh).total_seconds() < 15 * 60:
        return _last_shock_result

    headlines = latest_macro_headlines(limit_per_ticker=5)
    shock_result = scan_for_macro_shock(headlines)
    _last_shock_refresh = now
    _last_shock_result = shock_result

    if shock_result.get("shock_detected"):
        log_event("SIGNAL", "macro_shock_detected", shock_result)
        _send_discord_alert(
            "Macro shock detected\n"
            f"Classification: {shock_result.get('classification')}\n"
            f"Direction: {shock_result.get('direction')}\n"
            f"Affected: {', '.join(shock_result.get('affected_sectors') or [])}\n"
            f"Reason: {shock_result.get('reason')}"
        )
    elif "unavailable" in str(shock_result.get("reason", "")):
        log_event("WARN", "macro_shock_scan_unavailable", shock_result)
    return shock_result


def _missing_runtime_config() -> list[str]:
    required = [
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "GROQ_API_KEY",
        "SUPABASE_URL",
        "SUPABASE_ANON_KEY",
        "SUPABASE_SERVICE_KEY",
    ]
    if not IS_PAPER_TRADING and os.getenv("ENABLE_LIVE_TRADING", "").strip().lower() != "true":
        required.append("ENABLE_LIVE_TRADING")
    return [key for key in required if not os.getenv(key)]


def _apply_execution_overrides(profile: dict) -> dict:
    p = profile.copy()
    if IS_PAPER_TRADING:
        for key, value in p.get("paper_overrides", {}).items():
            p[key] = value
    short_override = os.getenv("ALLOW_SHORT_SELLING")
    if short_override is not None and short_override.strip():
        p["allow_short_selling"] = short_override.strip().lower() == "true"
    return p


def _apply_learned_hold_extension(
    ticker: str,
    hold_minutes: int,
    conviction: float,
    composite: float,
    profile: dict,
    portfolio_state: dict,
) -> tuple[int, Optional[dict]]:
    """
    Extend high-quality intraday trades based on the latest learning that
    longer holds have produced better net P&L.
    """
    if not _env_bool("LEARNED_HOLD_EXTENSION_ENABLED", True):
        return hold_minutes, None

    min_conviction = _env_float(
        "LEARNED_HOLD_MIN_CONVICTION",
        float(profile.get("learned_hold_min_conviction", 0.80)),
    )
    min_score = _env_float(
        "LEARNED_HOLD_MIN_SIGNAL_SCORE",
        float(profile.get("learned_hold_min_signal_score", 0.35)),
    )
    if conviction < min_conviction or abs(composite) < min_score:
        return hold_minutes, None

    vix = float(portfolio_state.get("vix") or 20.0)
    vix_ceiling = float(profile.get("vix_ceiling", 50))
    if vix > vix_ceiling:
        return hold_minutes, None

    min_minutes = _env_int(
        "LEARNED_HOLD_MIN_MINUTES",
        int(profile.get("learned_hold_min_minutes", 120)),
    )
    max_minutes = _env_int(
        "LEARNED_HOLD_MAX_MINUTES",
        int(profile.get("learned_hold_max_minutes", 240)),
    )
    confidence_span = max(
        0.0,
        min(1.0, (conviction - min_conviction) / max(1.0 - min_conviction, 0.01)),
    )
    target_minutes = int(min_minutes + (max_minutes - min_minutes) * confidence_span)
    extended_hold = max(int(hold_minutes), min(max_minutes, max(min_minutes, target_minutes)))
    if extended_hold == hold_minutes:
        return hold_minutes, None

    return extended_hold, {
        "ticker": ticker.upper(),
        "previous_hold_minutes": int(hold_minutes),
        "extended_hold_minutes": int(extended_hold),
        "conviction": round(float(conviction), 3),
        "composite": round(float(composite), 4),
        "min_conviction": min_conviction,
        "min_signal_score": min_score,
        "vix": round(vix, 2),
        "source": "learning_longer_holds_all_tickers",
    }


def _trading_capital(equity: float) -> float:
    raw = os.getenv("TRADING_CAPITAL_EUR") or os.getenv("STARTING_CAPITAL_EUR")
    if raw and raw.strip():
        try:
            return min(float(raw), equity)
        except ValueError:
            pass
    return equity


def _deterministic_action(composite: float) -> str:
    return "BUY" if composite > 0 else "SELL"


def _cap_short_notional(size_eur: float, capital_base: float, profile: dict) -> float:
    short_cap_pct = profile.get("max_short_position_pct")
    if short_cap_pct is None:
        short_cap_pct = profile.get("max_position_pct", 0)
    return min(size_eur, capital_base * float(short_cap_pct) / 100)


_INVERSE_ETFS = {"SH", "PSQ", "SQQQ", "SPXU", "SDS", "QID", "DOG", "TZA"}
_DEFENSIVE_TICKERS = {"GLD", "TLT", "IEF", "SHY", "SGOV", "BIL"}


def _exposure_direction(ticker: str, side: str) -> str:
    ticker = str(ticker or "").upper()
    side = str(side or "").upper()
    if side == "SELL":
        return "short_market"
    if ticker in _INVERSE_ETFS:
        return "short_market"
    if ticker in _DEFENSIVE_TICKERS:
        return "defensive_long"
    return "long_market"


def _regime_debug_payload(regime_state, signal_result: dict = None) -> dict:
    signal_result = signal_result or {}
    payload = regime_state.to_dict() if hasattr(regime_state, "to_dict") else {}
    payload.update({
        "macro_regime": signal_result.get("macro_regime"),
        "macro_multiplier": signal_result.get("macro_multiplier", 1.0),
        "regime_bull_bear": signal_result.get("regime_bull_bear"),
        "shock_detected": signal_result.get("shock_detected", False),
        "shock_classification": signal_result.get("shock_classification"),
    })
    return payload


def _strategy_family(ticker: str, side: str, regime: str, signal_result: dict,
                     horizon: str = "short", mean_reversion_trade: bool = False) -> str:
    ticker = str(ticker or "").upper()
    side = str(side or "").upper()
    regime = str(regime or "")
    horizon = str(horizon or "short")
    signal_result = signal_result or {}
    signals = signal_result.get("signals", {}) or {}

    if mean_reversion_trade or signal_result.get("mean_reversion_signal"):
        return "mean_reversion"
    if horizon == "swing":
        return "swing"
    if ticker in _INVERSE_ETFS:
        return "inverse_etf"
    if side == "SELL":
        return "direct_short"

    macd = signals.get("macd_crossover", {}).get("score", 0)
    rel_strength = signals.get("relative_strength", {}).get("score", 0)
    tape = signals.get("tape_aggression", {}).get("score", 0)
    if regime == "trending" or max(macd, rel_strength, tape) >= 0.35:
        return "trend_following"
    return "signal_composite"


def _log_short_candidate(event_name: str, ticker: str, composite: float,
                         reason: str = None, profile: dict = None,
                         regime_state = None, extra: dict = None):
    profile = profile or {}
    market_regime = getattr(regime_state, "market_regime", None)
    min_short_score = profile.get("min_short_signal_score", profile.get("min_signal_score"))
    if str(market_regime or "").lower() == "bull":
        min_short_score = profile.get("bull_short_signal_score", min_short_score)
    payload = {
        "ticker": ticker,
        "composite": composite,
        "reason": reason,
        "market_regime": market_regime,
        "intraday_regime": getattr(regime_state, "intraday_regime", None),
        "min_signal_score": profile.get("min_signal_score"),
        "min_short_signal_score": min_short_score,
        "allow_short_selling": profile.get("allow_short_selling", False),
    }
    if extra:
        payload.update(extra)
    log_event("INFO", event_name, payload)


def _parse_dt(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if not value:
        return datetime.utcnow()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return datetime.utcnow()


def _trade_pnl_pct(trade: dict, current_price: float) -> float:
    entry_price = float(trade.get("entry_price") or 0)
    if entry_price <= 0:
        return 0.0
    pnl_pct = (current_price - entry_price) / entry_price * 100
    if trade.get("side") == "SELL":
        pnl_pct = -pnl_pct
    return pnl_pct


def _directional_score(side: str, composite: float) -> float:
    return float(composite) if side == "BUY" else -float(composite)


def _time_exit_cooldown_active(ticker: str, recent_trades: list, profile: dict) -> Optional[dict]:
    cooldown_minutes = _env_int(
        "TIME_EXIT_COOLDOWN_MINUTES",
        int(profile.get("time_exit_cooldown_minutes", 60)),
    )
    if cooldown_minutes <= 0:
        return None

    now = datetime.utcnow()
    latest = None
    for trade in recent_trades or []:
        if str(trade.get("ticker", "")).upper() != ticker.upper():
            continue
        if trade.get("exit_reason") != "time_exit":
            continue
        closed_at = _parse_dt(
            trade.get("exit_time") or trade.get("created_at") or trade.get("closed_at")
        )
        if latest is None or closed_at > latest:
            latest = closed_at

    if latest is None:
        return None
    elapsed = (now - latest).total_seconds() / 60
    if elapsed < cooldown_minutes:
        return {
            "ticker": ticker,
            "minutes_since_time_exit": round(elapsed, 1),
            "cooldown_minutes": cooldown_minutes,
        }
    return None


def _hydrate_open_trades():
    for record in get_open_trade_records():
        ticker = record.get("ticker")
        if not ticker or ticker in _open_trades:
            continue
        _open_trades[ticker] = {
            "entry_time": _parse_dt(record.get("entry_time") or record.get("created_at")),
            "entry_price": float(record.get("entry_price") or 0),
            "stop_price": float(record.get("stop_price") or 0),
            "take_profit_price": float(record.get("take_profit_price") or 0),
            "hold_minutes": int(record.get("hold_minutes") or 30),
            "hold_days": int(record.get("hold_days") or 0),
            "horizon": record.get("horizon") or "short",
            "size_eur": float(record.get("size_eur") or 0),
            "size_usd": float(record.get("size_usd") or 0),
            "side": record.get("side", "BUY"),
            "composite_score": float(record.get("composite_score") or 0),
            "signals_json": record.get("signals_json") or {},
            "regime": record.get("regime") or "ranging",
            "exposure_direction": record.get("exposure_direction"),
            "strategy_family": record.get("strategy_family"),
            "regime_debug_json": record.get("regime_debug_json") or {},
            "macro_regime": record.get("macro_regime"),
            "macro_multiplier": float(record.get("macro_multiplier") or 1.0),
            "dip_type": record.get("dip_type"),
            "sizing_json": record.get("sizing_json") or {},
            "mean_reversion_trade": bool(record.get("mean_reversion_trade") or False),
            "swing_trade": bool(record.get("swing_trade") or False),
            "promoted_to_swing": bool(record.get("promoted_to_swing") or False),
            "promoted_at": record.get("promoted_at"),
            "initial_horizon": record.get("initial_horizon") or record.get("horizon") or "short",
            "swing_conviction": float(record.get("swing_conviction") or 0),
            "swing_reasons": record.get("swing_reasons") or [],
            "highest_price_since_entry": float(
                record.get("highest_price_since_entry") or record.get("entry_price") or 0
            ),
            "trailing_stop_price": float(record.get("trailing_stop_price") or 0),
            "stop_multiplier": float(record.get("stop_multiplier") or 1.5),
            "stop_pct": float(record.get("stop_pct") or 0),
            "max_hold_minutes": int(record.get("max_hold_minutes") or record.get("hold_minutes") or 30),
            "daily_reeval_count": int(record.get("daily_reeval_count") or 0),
            "hold_extension_count": int(record.get("hold_extension_count") or 0),
            "hold_decision_json": record.get("hold_decision_json") or {},
            "peak_directional_score": float(record.get("peak_directional_score") or 0),
            "protective_stop_order_id": record.get("protective_stop_order_id"),
            "llm_conviction": float(record.get("llm_conviction") or 0),
            "llm_rationale": record.get("llm_rationale") or "",
            "order_id": record.get("order_id"),
        }


# ── PDT (Pattern Day Trader) tracking ────────────────────────────────────────

def _record_day_trade(ticker: str):
    """Record a same-day round trip for PDT monitoring."""
    _day_trade_log.append((datetime.utcnow().date(), ticker))


def _count_day_trades_5d() -> int:
    """Count round trips (same-day open + close) in the last 5 calendar days."""
    cutoff = datetime.utcnow().date() - timedelta(days=7)
    return sum(1 for d, _ in _day_trade_log if d >= cutoff)


def _check_pdt_warning(ticker: str, count: int):
    log_event("WARN", "pdt_warning", {
        "day_trade_count_5d": count,
        "trigger_ticker": ticker,
        "note": "Approaching 4-round-trip PDT limit",
    })
    _send_discord_alert(
        f"PDT WARNING: {count} day trades in 5 days "
        f"(last: {ticker}). Stop at 3 to avoid PDT violation on live account."
    )


# ── Momentum swing promotion ──────────────────────────────────────────────────

def _try_promote_to_swing(ticker: str, trade: dict, current_price: float,
                          profile: dict, regime_state=None) -> bool:
    """
    Called at intraday time_exit boundary. If momentum is still intact and
    the trade is profitable, promote it to a 3-5 day swing instead of closing.
    Returns True if promoted (caller should skip the close).
    """
    # Must be profitable to promote
    if (trade.get("entry_price") or 0) <= 0:
        return False
    pnl_pct = _trade_pnl_pct(trade, current_price)
    if pnl_pct <= 0:
        return False

    # Don't promote mean-reversion trades
    if trade.get("mean_reversion_trade"):
        return False

    # Check concurrent swing limit before running expensive signal computation
    open_swing_count = sum(1 for d in _open_trades.values() if d.get("swing_trade"))
    max_swings = int(profile.get("max_concurrent_swings", 2))
    if open_swing_count >= max_swings:
        log_event("INFO", "swing_promotion_blocked_concurrent", {
            "ticker": ticker,
            "open_swings": open_swing_count,
            "max_swings": max_swings,
        })
        return False

    try:
        weights = _learning_engine.get_weights("trending") if _learning_engine else profile["signal_weights"]
        regime_state = regime_state or detect_regime(ticker)
        signal_result = _get_cached_signals(ticker, weights, regime_state)
        swing_check = detect_momentum_swing(ticker, signal_result, regime_state, profile)
    except Exception as e:
        log_event("WARN", "swing_promotion_signal_error", {"ticker": ticker, "error": str(e)[:80]})
        return False

    if not swing_check.get("swing_detected"):
        return False

    hold_days = swing_check["hold_days"]
    hold_minutes = swing_check["hold_minutes"]
    stop_multiplier = swing_check["stop_multiplier"]

    atr_data = signal_result.get("atr_data", {})
    atr_pct = atr_data.get("atr_pct") or 2.5
    atr_raw = atr_data.get("atr_raw") or (entry_price * atr_pct / 100)
    stop_pct = max(0.5, min(12.0, float(atr_pct) * stop_multiplier))

    side = trade.get("side", "BUY")
    if side == "BUY":
        stop_price = entry_price * (1 - stop_pct / 100)
        chandelier_stop = current_price - (atr_raw * stop_multiplier)
        protective_stop_price = max(stop_price, chandelier_stop)
        protective_side = "sell"
    else:
        stop_price = entry_price * (1 + stop_pct / 100)
        chandelier_stop = current_price + (atr_raw * stop_multiplier)
        protective_stop_price = min(stop_price, chandelier_stop)
        protective_side = "buy"

    cancel_results = _cancel_bracket_orders_for_manual_exit(trade)
    cancel_errors = [r for r in cancel_results if r.get("error")]
    if cancel_errors:
        log_event("WARN", "swing_promotion_bracket_cancel_failed", {
            "ticker": ticker,
            "errors": cancel_errors[:4],
        })
        return False

    protective_order = submit_stop_order(
        ticker=ticker,
        side=protective_side,
        qty=float(trade.get("quantity") or 0),
        stop_price=protective_stop_price,
        time_in_force="gtc",
    )
    if protective_order.get("error"):
        log_event("WARN", "swing_promotion_stop_order_failed", {
            "ticker": ticker,
            "error": protective_order["error"],
            "stop_price": round(protective_stop_price, 4),
        })
        return False

    _open_trades[ticker].update({
        "swing_trade":              True,
        "promoted_to_swing":        True,
        "promoted_at":              datetime.utcnow().isoformat(),
        "initial_horizon":          trade.get("horizon", "short"),
        "horizon":                  "swing",
        "hold_minutes":             hold_minutes,
        "max_hold_minutes":         hold_minutes,
        "stop_multiplier":          stop_multiplier,
        "swing_conviction":         swing_check["conviction"],
        "swing_reasons":            swing_check["reasons"],
        "highest_price_since_entry": max(current_price, entry_price),
        "trailing_stop_price":      round(protective_stop_price, 4),
        "stop_price":               round(protective_stop_price, 4),
        "stop_pct":                 stop_pct,
        "protective_stop_order_id": protective_order.get("order_id"),
        "hold_decision_json": {
            "promoted_at_pnl_pct": round(pnl_pct, 3),
            "swing_check":         swing_check,
            "cancelled_bracket_legs": cancel_results,
            "protective_stop_order": protective_order,
        },
    })

    try:
        save_open_trade(ticker, _open_trades[ticker])
    except Exception:
        pass

    log_event("INFO", "swing_promoted", {
        "ticker":          ticker,
        "hold_days":       hold_days,
        "conviction":      swing_check["conviction"],
        "reasons":         swing_check["reasons"],
        "pnl_at_promotion": round(pnl_pct, 3),
        "protective_stop_order_id": protective_order.get("order_id"),
    })
    _send_discord_alert(
        f"Swing promoted: {ticker} "
        f"{hold_days}-day hold · "
        f"Conviction: {swing_check['conviction']:.0%} · "
        f"P&L at promotion: {pnl_pct:+.1f}%"
    )
    return True


# ── Core cycle ────────────────────────────────────────────────────────────────

def run_signal_cycle():
    """Main cycle: compute signals → gate → decide → execute."""
    global _learning_engine

    missing_config = _missing_runtime_config()
    if missing_config:
        log_event("ERROR", "runtime_config_missing", {
            "missing": missing_config,
            "hint": "Set these as GitHub Actions secrets before the agent can trade.",
        })
        return

    if _learning_engine is None:
        _learning_engine = _init_learning_engine()

    portfolio_state = _get_portfolio_state()
    if portfolio_state.get("broker_error"):
        log_event("ERROR", "broker_account_unavailable", {
            "error": portfolio_state["broker_error"],
        })
        return
    regime_state    = detect_regime()
    regime          = regime_state.intraday_regime
    shock_result    = _refresh_macro_shock_if_needed()
    macro_regime, macro_meta = detect_macro_regime(return_meta=True)
    effective_profile = _apply_execution_overrides(
        get_effective_profile(PROFILE, portfolio_state)
    )
    weights          = _learning_engine.get_weights(regime)
    recent_trades    = get_recent_trades(days=30)
    _hydrate_open_trades()

    log_event("INFO", "cycle_start", {
        "regime": regime, "equity": portfolio_state["equity"],
        "vix": portfolio_state["vix"], "tickers": TICKERS,
        "horizon": HORIZON, "macro_regime": macro_regime,
        "macro_meta": macro_meta,
        "regime_state": regime_state.to_dict(),
        "shock_result": shock_result,
    })

    if not TICKERS:
        log_event("ERROR", "no_tickers_configured", {
            "hint": "Set TICKER_UNIVERSE or leave it unset to use SPY,QQQ,GLD"
        })
        return

    # Recall sweep if cash was parked and we're about to trade
    if has_active_sweep():
        recall_result = recall_sweep(reason="signal_cycle_starting")
        if recall_result.get("mode") == "simulation":
            log_event("INFO", "recall_simulated", recall_result)

    # Dividend opportunity scan (1hr cache — does not re-fetch per-ticker)
    if os.getenv("DIVIDEND_SCAN_ENABLED", "true").lower() == "true":
        try:
            div_opps = scan_dividend_calendar(TICKERS)
            for opp in div_opps:
                if opp.get("opportunity_score", 0) > 0.5:
                    log_dividend_opportunity(opp)
                    log_event("INFO", "dividend_opportunity", opp)
        except Exception as e:
            log_event("WARN", "dividend_scan_failed", {"error": str(e)[:100]})

    if _allows_intraday():
        for ticker in TICKERS:
            try:
                _process_ticker(ticker, regime, weights, effective_profile,
                                portfolio_state, recent_trades,
                                regime_state, shock_result)
            except Exception as e:
                log_event("ERROR", f"ticker_error_{ticker}", {"error": str(e)})

    _run_dip_buy_scan(TICKERS, portfolio_state, macro_regime, effective_profile, regime_state)

    if _should_run_swing_recheck():
        run_swing_cycle(
            portfolio_state=portfolio_state,
            profile=effective_profile,
            regime=regime,
            regime_state=regime_state,
            macro_regime=macro_regime,
        )

    # Check open trades for stop-loss / time exit
    _check_exits(portfolio_state, effective_profile)

    # Save portfolio snapshot
    _save_snapshot(portfolio_state, regime)


def _process_ticker(ticker, regime, weights, profile, portfolio_state, recent_trades,
                    regime_state, shock_result):
    """Signal → gate → EV → LLM → order."""
    ticker_regime_state = detect_regime(ticker)
    ticker_regime = ticker_regime_state.intraday_regime
    if _learning_engine:
        weights = _learning_engine.get_weights(ticker_regime)
    action_hint = None

    # 1. Compute signals (also warms the intra-cycle cache)
    signal_result = compute_all_signals(
        ticker, weights, regime_state=ticker_regime_state, shock_result=shock_result
    )
    _signal_cache[ticker] = (datetime.now(timezone.utc), signal_result)
    composite     = signal_result["composite_score"]
    signals_snap  = signal_result["signals"]
    atr_data       = signal_result.get("atr_data") or {}
    regime_debug   = _regime_debug_payload(ticker_regime_state, signal_result)
    news_headline = (signals_snap.get("news_sentiment", {})
                    .get("meta", {}).get("latest_headline", ""))

    # 2. Pre-trade gate (hard rules)
    capital_base = _trading_capital(portfolio_state["equity"])
    action_hint = _deterministic_action(composite)
    pre_size = compute_position_size(ticker, capital_base, profile, 0.7, atr_data, ticker_regime_state)
    size_eur = pre_size["size_eur"]
    if action_hint == "SELL":
        size_eur = _cap_short_notional(size_eur, capital_base, profile)
    cooldown = _time_exit_cooldown_active(ticker, recent_trades, profile)
    if cooldown:
        log_event("INFO", "time_exit_cooldown", cooldown)
        return
    gate_ok, gate_reason = pre_trade_gate(
        ticker, action_hint.lower(), size_eur, composite, profile, portfolio_state,
        market_regime=getattr(ticker_regime_state, "market_regime", None),
        signals=signals_snap,
    )

    # 3. Log signal to DB
    insert_signal({
        "ticker":                 ticker,
        "composite_score":        composite,
        "order_book_score":       signals_snap.get("order_book_imbalance", {}).get("score", 0),
        "tape_aggression_score":  signals_snap.get("tape_aggression", {}).get("score", 0),
        "rsi_divergence_score":   signals_snap.get("rsi_divergence", {}).get("score", 0),
        "news_sentiment_score":   signals_snap.get("news_sentiment", {}).get("score", 0),
        "vwap_deviation_score":   signals_snap.get("vwap_deviation", {}).get("score", 0),
        "macd_score":             signals_snap.get("macd_crossover", {}).get("score", 0),
        "rel_strength_score":     signals_snap.get("relative_strength", {}).get("score", 0),
        "bollinger_score":        signals_snap.get("bollinger_squeeze", {}).get("score", 0),
        "put_call_score":         signals_snap.get("put_call_ratio", {}).get("score", 0),
        "atr_pct":                atr_data.get("atr_pct"),
        "atr_stop_pct":           atr_data.get("suggested_stop_pct"),
        "volatility_regime":      atr_data.get("volatility_regime"),
        "earnings_days":          signals_snap.get("earnings_proximity", {}).get("meta", {}).get("days_to_earnings"),
        "earnings_mult":          signals_snap.get("earnings_proximity", {}).get("meta", {}).get("earnings_multiplier", 1.0),
        "macro_regime":           signal_result.get("macro_regime"),
        "macro_multiplier":       signal_result.get("macro_multiplier", 1.0),
        "regime_bull_bear":       signal_result.get("regime_bull_bear"),
        "shock_detected":         signal_result.get("shock_detected", False),
        "shock_classification":   signal_result.get("shock_classification"),
        "regime":                 ticker_regime,
        "action_hint":            action_hint,
        "exposure_direction":     _exposure_direction(ticker, action_hint),
        "strategy_family":        _strategy_family(ticker, action_hint, ticker_regime, signal_result),
        "regime_debug_json":      regime_debug,
        "vix":                    portfolio_state["vix"],
        "gated":                  not gate_ok,
        "gate_reason":            gate_reason if not gate_ok else None,
        "llm_called":             False,
    })

    if not gate_ok:
        log_event("INFO", "trade_gated", {
            "ticker": ticker,
            "composite": composite,
            "reason": gate_reason,
        })
        if action_hint == "SELL":
            _log_short_candidate(
                "short_candidate_gated", ticker, composite, gate_reason,
                profile, ticker_regime_state,
            )
        return

    # 4. EV check
    ev_result = compute_expected_value(composite, size_eur, recent_trades, ticker_regime)
    if ev_result["decision"] == "block":
        log_event("INFO", "ev_blocked", {"ticker": ticker, **ev_result})
        if action_hint == "SELL":
            _log_short_candidate(
                "short_candidate_ev_blocked", ticker, composite,
                ev_result.get("reason", "ev_blocked"), profile, ticker_regime_state, ev_result,
            )
        return

    # 5. LLM decision (gated by hourly limit)
    if not _can_call_llm():
        log_event("WARN", "llm_limit_hit", {"ticker": ticker})
        return

    llm_result = llm_signal_decision(
        ticker, composite, ticker_regime, news_headline, profile,
        signal_scores   = signals_snap,
        atr_data        = atr_data,
        regime_context  = {
            "market_regime": getattr(ticker_regime_state, "market_regime", ""),
            "vix":           portfolio_state.get("vix", ""),
        },
    )
    _record_llm_call()
    suggested_action = str(llm_result.get("action", "HOLD")).upper()
    action = _deterministic_action(composite)
    raw_llm_conviction = llm_result.get("conviction", 0)
    llm_conviction = raw_llm_conviction if isinstance(raw_llm_conviction, (int, float)) else 0
    conviction = max(abs(composite), float(llm_conviction or 0))
    log_event("SIGNAL", "llm_decision", {
        "ticker": ticker,
        "composite": composite,
        "deterministic_action": action,
        "llm_action": suggested_action,
        "conviction": conviction,
        "rationale": llm_result.get("rationale", ""),
    })

    if suggested_action == "HOLD":
        log_event("INFO", "llm_hold_veto", {
            "ticker": ticker,
            "composite": composite,
            "rationale": llm_result.get("rationale", ""),
        })
        if action == "SELL":
            _log_short_candidate(
                "short_candidate_llm_hold", ticker, composite,
                llm_result.get("rationale", "llm_hold"), profile, ticker_regime_state,
                {"llm_conviction": llm_conviction},
            )
        return
    if suggested_action in {"BUY", "SELL"} and suggested_action != action:
        log_event("INFO", "llm_direction_conflict", {
            "ticker": ticker,
            "composite": composite,
            "deterministic_action": action,
            "llm_action": suggested_action,
            "rationale": llm_result.get("rationale", ""),
        })
        if action == "SELL":
            _log_short_candidate(
                "short_candidate_llm_conflict", ticker, composite,
                llm_result.get("rationale", "llm_direction_conflict"),
                profile, ticker_regime_state,
                {"llm_action": suggested_action, "llm_conviction": llm_conviction},
            )
        return

    if conviction < profile["min_conviction"]:
        log_event("INFO", "conviction_below_threshold", {
            "ticker": ticker,
            "conviction": conviction,
            "min_conviction": profile["min_conviction"],
        })
        if action == "SELL":
            _log_short_candidate(
                "short_candidate_conviction_blocked", ticker, composite,
                "conviction_below_threshold", profile, ticker_regime_state,
                {"conviction": conviction, "min_conviction": profile["min_conviction"]},
            )
        return

    # 6. Size and submit order
    sizing = compute_position_size(ticker, capital_base, profile, conviction, atr_data, ticker_regime_state)
    final_size = sizing["size_eur"]
    if action == "SELL":
        final_size = _cap_short_notional(final_size, capital_base, profile)
    max_notional = _env_float(
        "MAX_NOTIONAL_PER_TRADE_EUR",
        profile.get("max_trade_notional_eur", final_size),
    )
    final_size = min(final_size, max_notional)
    sizing["size_eur"] = round(final_size, 2)

    import yfinance as yf
    bar = yf.download(ticker, period="1d", interval="1m",
                      progress=False, auto_adjust=True)
    if bar.empty:
        log_event("WARN", "price_unavailable", {"ticker": ticker})
        return
    current_price = float(bar["Close"].squeeze().iloc[-1])
    final_size_usd = _eur_to_usd(final_size)
    sizing["size_usd"] = round(final_size_usd, 2)
    qty = final_size_usd / current_price

    raw_hold_minutes = llm_result.get("hold_minutes", 30)
    try:
        hold_minutes = int(raw_hold_minutes)
    except (TypeError, ValueError):
        hold_minutes = 30
    mean_reversion_trade = bool(signal_result.get("mean_reversion_signal"))
    hold_extension = None
    if mean_reversion_trade:
        hold_minutes = 2880
    else:
        hold_minutes = max(
            int(profile.get("min_hold_minutes", 1)),
            min(int(profile.get("max_hold_minutes", 60)), hold_minutes),
        )
        hold_minutes, hold_extension = _apply_learned_hold_extension(
            ticker=ticker,
            hold_minutes=hold_minutes,
            conviction=conviction,
            composite=composite,
            profile=profile,
            portfolio_state=portfolio_state,
        )
        if hold_extension:
            log_event("INFO", "learned_hold_extended", hold_extension)

    stop_loss_pct = sizing.get("stop_pct") or float(llm_result.get("stop_loss_pct", profile["stop_loss_pct"]))
    if mean_reversion_trade:
        raw_atr_pct = atr_data.get("atr_pct")
        if raw_atr_pct:
            stop_loss_pct = max(stop_loss_pct, round(float(raw_atr_pct) * 2.0, 3))

    order = submit_market_order(
        ticker       = ticker,
        side         = action.lower(),
        qty          = round(qty, 6),
        stop_loss_pct= stop_loss_pct,
        take_profit_pct= profile.get("take_profit_pct", profile["stop_loss_pct"] * 1.2),
        current_price= current_price,
    )

    if "error" in order:
        log_event("ERROR", "order_failed", {"ticker": ticker, "error": order["error"]})
        return

    # Track open trade for exit monitoring
    if action == "BUY":
        stop_price = current_price * (1 - stop_loss_pct / 100)
        take_profit_price = current_price * (1 + profile.get("take_profit_pct", 2.0) / 100)
    else:
        stop_price = current_price * (1 + stop_loss_pct / 100)
        take_profit_price = current_price * (1 - profile.get("take_profit_pct", 2.0) / 100)

    exposure_direction = _exposure_direction(ticker, action)
    strategy_family = _strategy_family(
        ticker, action, ticker_regime, signal_result,
        horizon="short", mean_reversion_trade=mean_reversion_trade,
    )
    _open_trades[ticker] = {
        "entry_time":    datetime.utcnow(),
        "entry_price":   current_price,
        "quantity":      order.get("qty", round(qty, 6)),
        "stop_price":    stop_price,
        "take_profit_price": take_profit_price,
        "hold_minutes":  hold_minutes,
        "hold_extension_count": 0,
        "hold_decision_json": hold_extension,
        "size_eur":      final_size,
        "size_usd":      final_size_usd,
        "side":          action,
        "composite_score": composite,
        "signals_json":  {k: {"score": v["score"]} for k, v in signals_snap.items()},
        "regime":        ticker_regime,
        "exposure_direction": exposure_direction,
        "strategy_family": strategy_family,
        "regime_debug_json": regime_debug,
        "macro_regime":  signal_result.get("macro_regime"),
        "macro_multiplier": signal_result.get("macro_multiplier", 1.0),
        "horizon":       "short",
        "sizing_json":   sizing,
        "mean_reversion_trade": mean_reversion_trade,
        "swing_trade":   mean_reversion_trade,
        "llm_conviction": conviction,
        "llm_rationale": llm_result.get("rationale", ""),
        "order_id":      order.get("order_id"),
    }
    save_open_trade(ticker, _open_trades[ticker])

    log_event("TRADE", "order_submitted", {
        "ticker": ticker, "side": action,
        "size_eur": round(final_size, 2), "conviction": conviction,
        "composite": composite, "order_class": order.get("order_class"),
        "rationale": llm_result.get("rationale"),
        "sizing": sizing,
        "mean_reversion_trade": mean_reversion_trade,
        "exposure_direction": exposure_direction,
        "strategy_family": strategy_family,
    })


def _check_exits(portfolio_state, profile):
    """Check all open trades for stop-loss, chandelier stop, or time-based exit."""
    import yfinance as yf
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC for hold_elapsed arithmetic
    eod_cleanup = _is_eod_intraday_cleanup_window(datetime.now(timezone.utc))
    _regime_cache: dict[str, object] = {}  # per-call cache: ticker → regime_state

    for ticker, trade in list(_open_trades.items()):
        # Traditional horizon-swing trades (dip buys, ETF swings) are managed by
        # run_swing_cycle / re_evaluate_swing_positions — skip here.
        # Promoted momentum swings (promoted_to_swing=True) do get intraday
        # chandelier-stop checks.
        if trade.get("horizon") == "swing" and not trade.get("promoted_to_swing"):
            continue
        try:
            bar = yf.download(ticker, period="1d", interval="1m",
                             progress=False, auto_adjust=True)
            if bar.empty:
                continue
            current_price = float(bar["Close"].squeeze().iloc[-1])
            entry_time    = trade["entry_time"]
            hold_elapsed  = (now - entry_time).total_seconds() / 60
            hold_target   = trade.get("max_hold_minutes") or trade["hold_minutes"]

            exit_reason = None

            if trade.get("promoted_to_swing"):
                # Chandelier trailing stop: highest_since_entry - (ATR × stop_mult)
                entry_price = trade.get("entry_price", current_price)
                prev_highest = trade.get("highest_price_since_entry", entry_price)
                new_highest  = max(current_price, prev_highest)
                _open_trades[ticker]["highest_price_since_entry"] = new_highest

                atr_info = compute_atr(ticker)
                atr_raw  = atr_info.get("atr_raw") or (entry_price * 0.025)
                stop_mult = float(trade.get("stop_multiplier", 2.5))
                if trade.get("side", "BUY") == "BUY":
                    chandelier_stop = new_highest - (atr_raw * stop_mult)
                    old_stop = float(trade.get("trailing_stop_price") or trade.get("stop_price") or 0)
                    should_replace_stop = chandelier_stop > old_stop
                else:
                    chandelier_stop = min(current_price, prev_highest) + (atr_raw * stop_mult)
                    old_stop = float(trade.get("trailing_stop_price") or trade.get("stop_price") or 0)
                    should_replace_stop = old_stop <= 0 or chandelier_stop < old_stop

                if should_replace_stop:
                    stop_order = _replace_protective_stop_order(ticker, _open_trades[ticker], chandelier_stop)
                    if stop_order.get("error"):
                        log_event("WARN", "protective_stop_replace_failed", {
                            "ticker": ticker,
                            "error": stop_order["error"],
                            "stop_price": round(chandelier_stop, 4),
                        })
                        exit_reason = "circuit_breaker"
                    else:
                        save_open_trade(ticker, _open_trades[ticker])

                pnl_pct = _trade_pnl_pct(trade, current_price)
                if exit_reason:
                    pass
                elif trade.get("side", "BUY") == "BUY" and current_price <= chandelier_stop:
                    exit_reason = "chandelier_stop"
                elif trade.get("side") == "SELL" and current_price >= chandelier_stop:
                    exit_reason = "chandelier_stop"
                elif pnl_pct >= 8.0:
                    # Hard-coded take-profit — not configurable
                    exit_reason = "take_profit_8pct"
                # Time exit for promoted swings is handled by re_evaluate_swing_positions (daily)

            else:
                # Normal intraday stop/take-profit/time exit

                # For extended trades check momentum decay each cycle before stop/time checks
                if exit_reason is None and int(trade.get("hold_extension_count") or 0) > 0:
                    exit_reason = _check_momentum_exit(ticker, trade, profile)

                # Stop-loss and take-profit always evaluated first
                stop_price        = trade["stop_price"]
                take_profit_price = trade.get("take_profit_price")

                if trade["side"] == "SELL":
                    if current_price >= stop_price:
                        exit_reason = "stop_loss"
                    elif take_profit_price and current_price <= take_profit_price:
                        exit_reason = "take_profit"
                else:
                    if current_price <= stop_price:
                        exit_reason = "stop_loss"
                    elif take_profit_price and current_price >= take_profit_price:
                        exit_reason = "take_profit"

                if exit_reason is None:
                    if eod_cleanup:
                        # Near close: promote strong winners to swing, close everything else.
                        # Regime is cached per-call to avoid duplicate detect_regime fetches.
                        cached_regime = _regime_cache.get(ticker)
                        if cached_regime is None:
                            cached_regime = detect_regime(ticker)
                            _regime_cache[ticker] = cached_regime
                        if _try_promote_to_swing(ticker, trade, current_price, profile, cached_regime):
                            log_event("INFO", "eod_intraday_promoted", {
                                "ticker": ticker,
                                "pnl_pct": round(_trade_pnl_pct(trade, current_price), 4),
                            })
                            continue
                        pnl_pct_eod = _trade_pnl_pct(trade, current_price)
                        log_event("INFO", "eod_intraday_cleanup_exit", {
                            "ticker": ticker,
                            "pnl_pct": round(pnl_pct_eod, 4),
                            "outcome": "loser" if pnl_pct_eod < 0 else (
                                "flat" if pnl_pct_eod < 0.05 else "winner_not_swing_qualified"
                            ),
                        })
                        exit_reason = "eod_cleanup"
                    elif hold_elapsed >= hold_target:
                        exit_reason = _handle_hold_deadline(
                            ticker, trade, current_price, profile, portfolio_state
                        )
                        if exit_reason is None:
                            continue

            if exit_reason:
                _close_trade(ticker, trade, current_price, exit_reason)
        except Exception as e:
            log_event("ERROR", f"exit_check_{ticker}", {"error": str(e)})


def _handle_hold_deadline(
    ticker: str,
    trade: dict,
    current_price: float,
    profile: dict,
    portfolio_state: dict,
) -> Optional[str]:
    """
    Decide what to do when an intraday trade reaches its hold deadline.
    Time is treated as a risk signal: exit weak trades, extend aligned winners,
    and let strong breakouts attempt swing promotion.
    """
    pnl_pct = _trade_pnl_pct(trade, current_price)
    side = trade.get("side", "BUY")
    entry_score = _directional_score(side, float(trade.get("composite_score") or 0.0))

    try:
        regime_state = detect_regime(ticker)
        weights = (
            _learning_engine.get_weights(regime_state.intraday_regime)
            if _learning_engine else profile["signal_weights"]
        )
        signal_result = _get_cached_signals(ticker, weights, regime_state)
        current_composite = float(signal_result.get("composite_score") or 0.0)
    except Exception as e:
        log_event("WARN", "hold_deadline_signal_error", {
            "ticker": ticker,
            "error": str(e)[:100],
        })
        current_composite = 0.0
        signal_result = {}

    current_score = _directional_score(side, current_composite)
    fade_score = _env_float(
        "HOLD_EXTENSION_FADE_SCORE",
        float(profile.get("hold_extension_fade_score", 0.10)),
    )
    aligned_score = _env_float(
        "HOLD_EXTENSION_MIN_SIGNAL_SCORE",
        float(profile.get("hold_extension_min_signal_score", 0.20)),
    )
    min_green = _env_float(
        "HOLD_EXTENSION_MIN_PNL_PCT",
        float(profile.get("hold_extension_min_pnl_pct", 0.05)),
    )
    extension_minutes = _env_int(
        "HOLD_EXTENSION_MINUTES",
        int(profile.get("hold_extension_minutes", 30)),
    )
    max_extensions = _env_int(
        "HOLD_EXTENSION_MAX_COUNT",
        int(profile.get("hold_extension_max_count", 2)),
    )
    extensions_used = int(trade.get("hold_extension_count") or 0)
    weakened = current_score < fade_score or current_score < entry_score * 0.5

    decision = {
        "ticker": ticker,
        "pnl_pct": round(pnl_pct, 4),
        "entry_directional_score": round(entry_score, 4),
        "current_directional_score": round(current_score, 4),
        "extensions_used": extensions_used,
    }

    if pnl_pct <= 0 and weakened:
        decision["decision"] = "exit_losing_or_flat_weakened"
        log_event("INFO", "hold_deadline_exit", decision)
        return "time_exit"

    if 0 < pnl_pct < min_green and weakened:
        decision["decision"] = "exit_small_green_momentum_faded"
        log_event("INFO", "hold_deadline_exit", decision)
        return "time_exit"

    # Only attempt swing promotion if signal is still aligned (not faded)
    if pnl_pct > 0 and current_score >= aligned_score and signal_result.get("composite_score") is not None:
        if _try_promote_to_swing(ticker, trade, current_price, profile):
            decision["decision"] = "promoted_to_swing"
            log_event("INFO", "hold_deadline_promoted", decision)
            return None

    if pnl_pct >= min_green and current_score >= aligned_score and extensions_used < max_extensions:
        _open_trades[ticker]["hold_minutes"] = int(trade.get("hold_minutes") or 0) + extension_minutes
        _open_trades[ticker]["hold_extension_count"] = extensions_used + 1
        # Capture momentum baseline for decay tracking on first extension
        if extensions_used == 0:
            _open_trades[ticker]["peak_directional_score"] = round(current_score, 4)
        hold_decision = dict(trade.get("hold_decision_json") or {})
        hold_decision["deadline_extension"] = {
            **decision,
            "decision": "extend_aligned_winner",
            "extension_minutes": extension_minutes,
            "new_hold_minutes": _open_trades[ticker]["hold_minutes"],
        }
        _open_trades[ticker]["hold_decision_json"] = hold_decision
        save_open_trade(ticker, _open_trades[ticker])
        log_event("INFO", "hold_deadline_extended", hold_decision["deadline_extension"])
        return None

    decision["decision"] = "exit_deadline_no_edge"
    log_event("INFO", "hold_deadline_exit", decision)
    return "time_exit"


def _check_momentum_exit(ticker: str, trade: dict, profile: dict) -> Optional[str]:
    """
    Per-cycle momentum health check for extended intraday trades.
    Uses only the intra-cycle signal cache — zero extra API calls.

    Single signal: peak decay — directional score dropped >40% from its
    peak since extension was granted. Other signals (VWAP recross, volume
    fade, score persistence) are deferred until validated by trade data.
    """
    cached = _signal_cache.get(ticker)
    if cached is None:
        return None

    side = trade.get("side", "BUY")
    current_score = _directional_score(side, float(cached[1].get("composite_score") or 0))

    peak_score = float(trade.get("peak_directional_score") or current_score)
    if current_score > peak_score:
        _open_trades[ticker]["peak_directional_score"] = round(current_score, 4)
    elif peak_score > 0.10 and current_score < peak_score * 0.60:
        log_event("INFO", "momentum_exit_triggered", {
            "ticker": ticker,
            "peak_score": round(peak_score, 4),
            "current_score": round(current_score, 4),
        })
        return "momentum_peak_decay"

    return None


def _recover_bracket_fill(trade: dict) -> Optional[dict]:
    """
    Query Alpaca order history to recover exit price when a bracket leg
    (stop-loss or take-profit) was triggered autonomously by Alpaca.
    Returns {"exit_price", "exit_reason", "close_order_id"} or None.
    """
    order_id = trade.get("order_id")
    if not order_id:
        return None
    try:
        order = get_order_by_id(str(order_id))
        if "error" in order:
            return None
        for leg in order.get("legs", []):
            if "filled" not in str(leg.get("status", "")).lower():
                continue
            price = float(leg.get("filled_price") or 0)
            if not price:
                continue
            order_type = str(leg.get("type", "")).lower()
            # limit leg = take-profit; stop leg = stop-loss
            reason = "take_profit" if "limit" in order_type else "stop_loss"
            return {
                "exit_price":     price,
                "exit_reason":    reason,
                "close_order_id": leg.get("id"),
            }
    except Exception:
        pass
    return None


def _recover_protective_stop_fill(trade: dict) -> Optional[dict]:
    """Recover fill details when a standalone protective stop already closed the position."""
    order_id = trade.get("protective_stop_order_id")
    if not order_id:
        return None
    try:
        order = get_order_by_id(str(order_id))
        if "error" in order or "filled" not in str(order.get("status", "")).lower():
            return None
        price = float(order.get("filled_price") or 0)
        if not price:
            return None
        return {
            "exit_price": price,
            "exit_reason": "chandelier_stop",
            "close_order_id": order.get("id") or order_id,
        }
    except Exception:
        return None


def _cancel_protective_stop_order(trade: dict) -> Optional[dict]:
    order_id = trade.get("protective_stop_order_id")
    if not order_id:
        return None
    return cancel_order_by_id(str(order_id))


def _replace_protective_stop_order(ticker: str, trade: dict,
                                   stop_price: float) -> dict:
    """Replace the broker-side protective stop when a trailing stop ratchets."""
    cancel_result = _cancel_protective_stop_order(trade)
    if cancel_result and cancel_result.get("error"):
        return {"error": cancel_result["error"], "cancel_result": cancel_result}

    close_side = "sell" if trade.get("side", "BUY") == "BUY" else "buy"
    order = submit_stop_order(
        ticker=ticker,
        side=close_side,
        qty=float(trade.get("quantity") or 0),
        stop_price=stop_price,
        time_in_force="gtc",
    )
    if "error" not in order:
        trade["protective_stop_order_id"] = order.get("order_id")
        trade["stop_price"] = round(float(stop_price), 4)
        trade["trailing_stop_price"] = round(float(stop_price), 4)
    return order


def _cancel_bracket_orders_for_manual_exit(trade: dict) -> list[dict]:
    """
    Time exits can conflict with open bracket legs that reserve shares.
    Cancel those legs first, then submit the manual close.
    """
    order_id = trade.get("order_id")
    if not order_id:
        return []
    results = []
    order = get_order_by_id(str(order_id))
    if "error" in order:
        results.append({"order_id": order_id, "error": order["error"]})
        return results

    terminal = {"filled", "canceled", "cancelled", "expired", "rejected"}
    for leg in order.get("legs", []):
        status = str(leg.get("status", "")).lower()
        leg_id = leg.get("id")
        if not leg_id or any(state in status for state in terminal):
            continue
        results.append(cancel_order_by_id(str(leg_id)))

    if results:
        time.sleep(1)
    return results


def _close_trade(ticker: str, trade: dict, exit_price: float, exit_reason: str):
    """Close a position and record the trade outcome for learning."""
    global _learning_engine

    cancel_results = []
    protective_cancel = _cancel_protective_stop_order(trade)
    if protective_cancel:
        log_event("INFO", "protective_stop_cancelled_for_manual_exit", {
            "ticker": ticker,
            "result": protective_cancel,
        })

    if exit_reason == "time_exit":
        cancel_results = _cancel_bracket_orders_for_manual_exit(trade)
        if cancel_results:
            log_event("INFO", "bracket_orders_cancelled_for_time_exit", {
                "ticker": ticker,
                "results": cancel_results[:4],
            })

    result = close_position(ticker)
    close_error = result.get("error")
    if close_error:
        if "position not found" in str(close_error).lower():
            # Position was closed by broker-side protection — try to recover fill price.
            recovered = _recover_protective_stop_fill(trade) or _recover_bracket_fill(trade)
            if recovered:
                exit_price  = recovered["exit_price"]
                exit_reason = recovered["exit_reason"]
                close_error = None
                result      = {"order_id": recovered.get("close_order_id")}
                log_event("INFO", "bracket_fill_recovered", {
                    "ticker":       ticker,
                    "exit_price":   exit_price,
                    "exit_reason":  exit_reason,
                    "close_order":  recovered.get("close_order_id"),
                })
                # Fall through to normal trade recording below
            else:
                close_open_trade_record(ticker, "stale_no_position")
                if ticker in _open_trades:
                    del _open_trades[ticker]
                log_event("WARN", "stale_open_trade_unrecoverable", {
                    "ticker":      ticker,
                    "exit_reason": exit_reason,
                    "error":       close_error,
                    "note":        "Position not found and bracket fill could not be recovered.",
                })
                return
        else:
            log_event("WARN", "close_position_failed_recording_estimate", {
                "ticker": ticker,
                "error": close_error,
                "exit_reason": exit_reason,
            })
            return
    entry_price = trade["entry_price"]
    pnl_pct     = (exit_price - entry_price) / entry_price * 100
    if trade["side"] == "SELL":
        pnl_pct = -pnl_pct

    size_eur    = trade["size_eur"]
    size_usd    = trade.get("size_usd") or _eur_to_usd(size_eur)
    slippage    = size_eur * 0.0008   # Alpaca = $0 commission
    llm_cost    = 0.002
    net_pnl_pct = pnl_pct - (slippage + llm_cost) / size_eur * 100

    now_close = datetime.utcnow()
    hold_minutes_actual = int((now_close - trade["entry_time"]).total_seconds() / 60)
    hold_days_actual    = max(0, int((now_close - trade["entry_time"]).total_seconds() // 86400))

    # PDT tracking: same-day round trips on BUY trades
    entry_dt = trade.get("entry_time") or now_close
    if entry_dt.date() == now_close.date() and trade.get("side") == "BUY":
        _record_day_trade(ticker)
        day_trade_count = _count_day_trades_5d()
        log_event("INFO", "day_trade_recorded", {
            "ticker": ticker, "count_5d": day_trade_count,
        })
        if day_trade_count >= 3:
            _check_pdt_warning(ticker, day_trade_count)

    trade_record = {
        "ticker":          ticker,
        "side":            trade["side"],
        "entry_price":     round(entry_price, 4),
        "exit_price":      round(exit_price, 4),
        "quantity":        trade.get("quantity"),
        "stop_price":      round(float(trade.get("stop_price") or 0), 4),
        "take_profit_price": round(float(trade.get("take_profit_price") or 0), 4),
        "size_eur":        round(size_eur, 2),
        "size_usd":        round(size_usd, 2),
        "pnl_pct":         round(pnl_pct, 4),
        "net_pnl_pct":     round(net_pnl_pct, 4),
        "pnl_eur":         round(net_pnl_pct / 100 * size_eur, 2),
        "hold_minutes":    hold_minutes_actual,
        "hold_days_actual": hold_days_actual,
        "exit_reason":     exit_reason,
        "exit_trigger":    exit_reason,
        "regime":          trade["regime"],
        "macro_regime":    trade.get("macro_regime"),
        "macro_multiplier": trade.get("macro_multiplier"),
        "composite_score": trade["composite_score"],
        "llm_conviction":  trade["llm_conviction"],
        "llm_rationale":   trade["llm_rationale"],
        "signals_json":    trade["signals_json"],
        "exposure_direction": trade.get("exposure_direction") or _exposure_direction(ticker, trade["side"]),
        "strategy_family": trade.get("strategy_family"),
        "regime_debug_json": trade.get("regime_debug_json") or {},
        "dip_type":        trade.get("dip_type"),
        "sizing_json":     trade.get("sizing_json"),
        "mean_reversion_trade": bool(trade.get("mean_reversion_trade")),
        "swing_trade":     bool(trade.get("swing_trade") or trade.get("horizon") == "swing"),
        "promoted_to_swing": bool(trade.get("promoted_to_swing")),
        "promoted_at":     trade.get("promoted_at"),
        "initial_horizon": trade.get("initial_horizon") or trade.get("horizon") or HORIZON,
        "swing_conviction": trade.get("swing_conviction"),
        "swing_reasons":   trade.get("swing_reasons") or [],
        "stop_multiplier": trade.get("stop_multiplier"),
        "trailing_stop_price": trade.get("trailing_stop_price"),
        "highest_price_since_entry": trade.get("highest_price_since_entry"),
        "daily_reeval_count": int(trade.get("daily_reeval_count") or 0),
        "hold_extension_count": int(trade.get("hold_extension_count") or 0),
        "hold_decision_json": trade.get("hold_decision_json"),
        "protective_stop_order_id": trade.get("protective_stop_order_id"),
        "order_id":        trade.get("order_id"),
        "close_order_id":  result.get("order_id"),
        "close_error":     close_error,
        "commission_eur":  0.0,
        "slippage_eur":    round(slippage, 4),
        "llm_cost_eur":    llm_cost,
        "risk_profile":    PROFILE.get("_name", "moderate"),
        "horizon":         trade.get("horizon") or HORIZON,
        "atr_at_entry":    trade.get("atr_pct"),
        "stop_pct_used":   round(float(trade.get("stop_pct") or PROFILE.get("stop_loss_pct", 2.5)), 4),
        "r_multiple":      round(net_pnl_pct / max(float(trade.get("stop_pct") or PROFILE.get("stop_loss_pct", 2.5)), 0.1), 4),
    }

    insert_trade(trade_record)
    close_open_trade_record(ticker, exit_reason)
    del _open_trades[ticker]

    # Update learning engine
    attributions = attribute_signals(trade_record)
    if _learning_engine:
        _learning_engine.update(attributions, trade["regime"])
        weights = _learning_engine.all_weights()
        recent_count = sum(1 for _ in get_recent_trades(days=90))
        save_signal_weights(
            regime="global",
            weights=weights["global"],
            trade_count=recent_count,
            trigger="trade_update"
        )
        save_signal_weights(
            regime=trade["regime"],
            weights=weights.get(trade["regime"], weights["global"]),
            trade_count=recent_count,
            trigger="trade_update"
        )

    log_event("TRADE", "trade_closed", {
        "ticker": ticker, "net_pnl_pct": round(net_pnl_pct, 3),
        "exit_reason": exit_reason, "hold_min": trade_record["hold_minutes"]
    })


def _current_daily_price(ticker: str) -> float | None:
    try:
        import yfinance as yf
        bar = yf.download(ticker, period="5d", interval="1d",
                          progress=False, auto_adjust=True)
        if bar.empty:
            return None
        return float(bar["Close"].squeeze().iloc[-1])
    except Exception:
        return None


def _stop_pct_from_atr(ticker: str, multiplier: float, fallback: float) -> tuple[float, dict]:
    atr_data = compute_atr(ticker)
    atr_pct = atr_data.get("atr_pct")
    if atr_pct:
        return max(0.5, min(12.0, float(atr_pct) * multiplier)), atr_data
    return fallback, atr_data


def _submit_horizon_order(
    ticker: str,
    side: str,
    conviction: float,
    profile: dict,
    portfolio_state: dict,
    regime: str,
    horizon: str,
    stop_loss_pct: float,
    hold_days: int = None,
    hold_minutes: int = None,
    size_multiplier: float = 1.0,
    composite_score: float = 0.0,
    signals_json: dict = None,
    rationale: str = "",
    macro_regime: str = None,
    macro_multiplier: float = None,
    dip_type: str = None,
    regime_state = None,
    atr_data: dict = None,
    sizing_json: dict = None,
) -> dict:
    capital_base = _trading_capital(portfolio_state["equity"])
    regime_state = regime_state or detect_regime()
    sizing = sizing_json or compute_position_size(
        ticker, capital_base, profile, conviction, atr_data or {}, regime_state
    )
    size_eur = sizing["size_eur"] * size_multiplier
    if side.upper() == "SELL":
        size_eur = _cap_short_notional(size_eur, capital_base, profile)
    max_notional = _env_float(
        "MAX_NOTIONAL_PER_TRADE_EUR",
        profile.get("max_trade_notional_eur", size_eur),
    )
    size_eur = min(size_eur, max_notional)
    sizing["size_eur"] = round(size_eur, 2)

    current_price = _current_daily_price(ticker)
    if not current_price:
        log_event("WARN", "price_unavailable", {"ticker": ticker, "horizon": horizon})
        return {"error": "price_unavailable"}

    size_usd = _eur_to_usd(size_eur)
    sizing["size_usd"] = round(size_usd, 2)
    qty = size_usd / current_price
    take_profit_pct = profile.get("take_profit_pct", profile["stop_loss_pct"] * 1.2)
    order = submit_market_order(
        ticker=ticker,
        side=side.lower(),
        qty=round(qty, 6),
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        current_price=current_price,
    )
    if "error" in order:
        log_event("ERROR", "order_failed", {
            "ticker": ticker,
            "horizon": horizon,
            "error": order["error"],
        })
        return order

    if side.upper() == "BUY":
        stop_price = current_price * (1 - stop_loss_pct / 100)
        take_profit_price = current_price * (1 + take_profit_pct / 100)
    else:
        stop_price = current_price * (1 + stop_loss_pct / 100)
        take_profit_price = current_price * (1 - take_profit_pct / 100)

    signal_context = {
        "signals": signals_json or {},
        "macro_regime": macro_regime,
        "macro_multiplier": macro_multiplier,
    }
    exposure_direction = _exposure_direction(ticker, side)
    strategy_family = _strategy_family(
        ticker, side, regime, signal_context,
        horizon=horizon, mean_reversion_trade=False,
    )
    record = {
        "entry_time": datetime.utcnow(),
        "entry_price": current_price,
        "quantity": order.get("qty", round(qty, 6)),
        "stop_price": stop_price,
        "take_profit_price": take_profit_price,
        "hold_minutes": hold_minutes or 0,
        "hold_days": hold_days or 0,
        "size_eur": size_eur,
        "size_usd": size_usd,
        "side": side.upper(),
        "composite_score": composite_score,
        "signals_json": signals_json or {},
        "regime": regime,
        "exposure_direction": exposure_direction,
        "strategy_family": strategy_family,
        "regime_debug_json": _regime_debug_payload(regime_state, signal_context),
        "macro_regime": macro_regime,
        "macro_multiplier": macro_multiplier,
        "horizon": horizon,
        "dip_type": dip_type,
        "sizing_json": sizing,
        "mean_reversion_trade": False,
        "swing_trade": horizon == "swing",
        "llm_conviction": conviction,
        "llm_rationale": rationale,
        "order_id": order.get("order_id"),
    }
    _open_trades[ticker] = record
    save_open_trade(ticker, record)

    log_event("TRADE", "order_submitted", {
        "ticker": ticker,
        "side": side.upper(),
        "horizon": horizon,
        "size_eur": round(size_eur, 2),
        "conviction": conviction,
        "composite": composite_score,
        "order_class": order.get("order_class"),
        "rationale": rationale,
        "dip_type": dip_type,
        "sizing": sizing,
        "exposure_direction": exposure_direction,
        "strategy_family": strategy_family,
    })
    return order


def _run_dip_buy_scan(tickers: list[str], portfolio_state: dict, macro_regime: str,
                      profile: dict, regime_state=None):
    opportunities = scan_for_extreme_dips(tickers, portfolio_state, macro_regime)
    log_event("SIGNAL", "extreme_dip_scan_complete", {
        "macro_regime": macro_regime,
        "tickers_scanned": len(tickers),
        "opportunities": len(opportunities),
    })

    open_tickers = _open_position_tickers(portfolio_state) | set(_open_trades.keys())
    for opp in opportunities:
        log_event("SIGNAL", "extreme_dip_detected", opp)
        ticker = opp["ticker"]
        if ticker in open_tickers:
            log_event("INFO", "extreme_dip_skipped_open_position", {
                "ticker": ticker,
                "dip_score": opp.get("dip_score"),
            })
            continue
        stop_pct, atr_data = _stop_pct_from_atr(
            ticker,
            multiplier=opp.get("stop_multiplier", 2.0),
            fallback=profile.get("stop_loss_pct", 2.0) * 2,
        )
        order = _submit_horizon_order(
            ticker=ticker,
            side="BUY",
            conviction=opp.get("conviction", 0.85),
            profile=profile,
            portfolio_state=portfolio_state,
            regime="news_driven" if macro_regime == "geopolitical_shock" else "ranging",
            horizon="swing",
            stop_loss_pct=stop_pct,
            hold_days=opp.get("hold_days", 3),
            size_multiplier=opp.get("size_multiplier", 1.5),
            composite_score=opp.get("dip_score", 0.0),
            signals_json={"extreme_dip": {"score": opp.get("dip_score", 0.0), "meta": opp}},
            rationale=f"{opp.get('type')} dip buy: {opp.get('pct_from_high')}% below 20d high, RSI {opp.get('rsi')}",
            macro_regime=macro_regime,
            macro_multiplier=1.0,
            dip_type=opp.get("type"),
            regime_state=regime_state,
            atr_data=atr_data,
        )
        if "error" not in order:
            open_tickers.add(ticker)


def re_evaluate_swing_positions():
    """
    Runs once per day at market open (09:35 EST / 14:35 UTC).
    Re-scores each promoted momentum swing position and decides:
    HOLD (extend), EXIT (close now), or TIGHTEN (trail stop on profit).
    """
    _hydrate_open_trades()
    open_swings = [
        t for t, data in _open_trades.items()
        if data.get("promoted_to_swing") is True
    ]

    if not open_swings:
        log_event("INFO", "swing_reeval_no_positions", {})
        return

    log_event("INFO", "swing_reeval_start", {"positions": open_swings})

    profile = _apply_execution_overrides(
        get_effective_profile(PROFILE, _get_portfolio_state())
    )
    weights = (_learning_engine.get_weights("trending")
               if _learning_engine else profile["signal_weights"])
    regime = detect_regime()

    for ticker in open_swings:
        try:
            pos = _open_trades[ticker]
            result = compute_all_signals(ticker, weights, regime_state=regime)
            composite = result["composite_score"]

            entry_price   = pos.get("entry_price", 0)
            current_price = _current_daily_price(ticker)
            if not current_price:
                continue
            pnl_pct = _trade_pnl_pct(pos, current_price)

            # Increment daily reeval counter
            _open_trades[ticker]["daily_reeval_count"] = int(pos.get("daily_reeval_count", 0)) + 1

            exit_reasons = []

            if regime.market_regime == "bear":
                exit_reasons.append("regime_turned_bear")

            if composite < -0.20:
                exit_reasons.append("momentum_reversed")

            earn = (result["signals"]
                    .get("earnings_proximity", {})
                    .get("meta", {}))
            days_to_earn = earn.get("days_to_earnings")
            if days_to_earn is not None and days_to_earn <= 1:
                exit_reasons.append("earnings_tomorrow")

            if result.get("shock_detected"):
                exit_reasons.append("macro_shock")

            if pnl_pct >= 8.0:
                # Hard-coded take-profit — prevents greed overriding discipline
                exit_reasons.append("take_profit_8pct")

            # Check max hold days
            entry_time = pos.get("entry_time") or datetime.utcnow()
            days_held  = (datetime.utcnow() - entry_time).days
            max_days   = pos.get("max_hold_minutes", 1950) // 390
            if days_held >= max_days:
                exit_reasons.append("time_exit")

            if exit_reasons:
                log_event("INFO", "swing_exit_triggered", {
                    "ticker":    ticker,
                    "reasons":   exit_reasons,
                    "pnl_pct":   round(pnl_pct, 3),
                    "composite": composite,
                })
                _close_trade(ticker, pos, current_price, exit_reasons[0])
                _send_discord_alert(
                    f"Swing exit: {ticker} "
                    f"P&L: {pnl_pct:+.1f}% "
                    f"Reason: {exit_reasons[0]}"
                )
                continue

            # Tighten stop if profitable — trail stop to lock in gains
            if pnl_pct > 3.0:
                old_stop_pct = float(pos.get("stop_pct", 2.5))
                new_stop_pct = old_stop_pct * 0.75
                _open_trades[ticker]["stop_pct"] = new_stop_pct
                log_event("INFO", "swing_stop_tightened", {
                    "ticker":       ticker,
                    "pnl_pct":      round(pnl_pct, 3),
                    "new_stop_pct": round(new_stop_pct, 3),
                })

            days_remaining = max(0, max_days - days_held)
            save_open_trade(ticker, _open_trades[ticker])
            log_event("INFO", "swing_hold_confirmed", {
                "ticker":          ticker,
                "composite":       composite,
                "pnl_pct":         round(pnl_pct, 3),
                "days_held":       days_held,
                "days_remaining":  days_remaining,
                "reeval_count":    _open_trades[ticker]["daily_reeval_count"],
            })

        except Exception as e:
            log_event("ERROR", "swing_reeval_error",
                      {"ticker": ticker, "error": str(e)[:80]})


def run_swing_cycle(portfolio_state: dict = None, profile: dict = None,
                    regime: str = None, macro_regime: str = None,
                    regime_state = None):
    """Daily swing re-evaluation and entry scan for SWING_TICKERS."""
    if not _allows_swing():
        return
    if not SWING_TICKERS:
        return

    portfolio_state = portfolio_state or _get_portfolio_state()
    if portfolio_state.get("broker_error"):
        log_event("ERROR", "swing_broker_account_unavailable", {
            "error": portfolio_state["broker_error"],
        })
        return

    profile = profile or _apply_execution_overrides(get_effective_profile(PROFILE, portfolio_state))
    regime_state = regime_state or detect_regime()
    regime = regime or regime_state.intraday_regime
    if macro_regime is None:
        macro_regime = detect_macro_regime()

    _hydrate_open_trades()
    open_tickers = _open_position_tickers(portfolio_state) | set(_open_trades.keys())
    log_event("INFO", "swing_cycle_start", {
        "tickers": SWING_TICKERS,
        "macro_regime": macro_regime,
        "open_tickers": sorted(open_tickers),
    })

    # Re-evaluate existing swing positions.
    for ticker, trade in list(_open_trades.items()):
        if trade.get("horizon") != "swing":
            continue
        current_price = _current_daily_price(ticker)
        if not current_price:
            continue
        score, meta = compute_swing_score(ticker)
        elapsed_days = max(0, (datetime.utcnow() - trade["entry_time"]).days)
        exit_reason = None
        if trade["side"] == "BUY":
            if current_price <= trade["stop_price"]:
                exit_reason = "stop_loss"
            elif trade.get("take_profit_price") and current_price >= trade["take_profit_price"]:
                exit_reason = "take_profit"
            elif score < -0.15:
                exit_reason = "signal_reversal"
        elif trade["side"] == "SELL" and score > 0.15:
            exit_reason = "signal_reversal"
        if elapsed_days >= int(trade.get("hold_days") or 3) and exit_reason is None:
            exit_reason = "time_exit"
        log_event("SIGNAL", "swing_recheck", {
            "ticker": ticker,
            "score": round(score, 4),
            "elapsed_days": elapsed_days,
            "hold_days": trade.get("hold_days"),
            "exit_reason": exit_reason,
            "meta": meta,
        })
        if exit_reason:
            _close_trade(ticker, trade, current_price, exit_reason)

    open_tickers = _open_position_tickers(portfolio_state) | set(_open_trades.keys())
    for ticker in SWING_TICKERS:
        if ticker in open_tickers:
            continue
        score, meta = compute_swing_score(ticker)
        action = _deterministic_action(score)
        if action == "SELL" and not profile.get("allow_short_selling", False):
            log_event("INFO", "swing_short_gated", {"ticker": ticker, "score": score})
            continue
        min_score = float(os.getenv("SWING_MIN_SCORE", profile.get("min_signal_score", 0.25)))
        if abs(score) < min_score:
            log_event("INFO", "swing_signal_below_threshold", {
                "ticker": ticker,
                "score": round(score, 4),
                "min_score": min_score,
            })
            continue
        stop_pct, atr_data = _stop_pct_from_atr(
            ticker,
            multiplier=2.0,
            fallback=profile.get("stop_loss_pct", 2.0) * 2,
        )
        conviction = max(0.65, min(0.90, abs(score)))
        _submit_horizon_order(
            ticker=ticker,
            side=action,
            conviction=conviction,
            profile=profile,
            portfolio_state=portfolio_state,
            regime=regime,
            horizon="swing",
            stop_loss_pct=stop_pct,
            hold_days=_env_int("SWING_HOLD_DAYS", 3),
            size_multiplier=1.0,
            composite_score=score,
            signals_json={"swing_score": {"score": score, "meta": meta}, "atr": {"score": 0, "meta": atr_data}},
            rationale="daily swing score entry",
            macro_regime=macro_regime,
            macro_multiplier=1.0,
            regime_state=regime_state,
            atr_data=atr_data,
        )


def _save_snapshot(portfolio_state, regime):
    from database.client import get_snapshots
    snaps = get_snapshots(days=1)
    equity = portfolio_state["equity"]

    # Baseline is STARTING_CAPITAL_EUR converted to USD so the comparison is
    # apples-to-apples: equity (USD-capped) vs start_usd.
    raw_capital = os.getenv("STARTING_CAPITAL_EUR", "3000")
    fx_rate     = float(os.getenv("EURUSD_RATE", "1.08") or "1.08")
    start_equity_usd = float(raw_capital) * fx_rate if raw_capital and raw_capital.strip() else equity

    cum_pnl = (equity - start_equity_usd) / start_equity_usd * 100 if start_equity_usd else 0.0
    cum_pnl = max(-9999.0, min(9999.0, cum_pnl))

    save_snapshot({
        "total_value_eur":    equity,
        "cash_eur":           portfolio_state["cash"],
        "daily_pnl_pct":      -portfolio_state["drawdown_today"],
        "cumulative_pnl_pct": round(cum_pnl, 3),
        "drawdown_pct":       portfolio_state["drawdown_today"],
        "open_positions":     portfolio_state["positions"],
        "trades_today":       portfolio_state["trades_today"],
        "llm_calls_today":    _llm_calls_this_hour,
        "llm_cost_today":     round(_llm_calls_this_hour * 0.001, 4),
    })


# ── Nightly sweep (after US market close) ────────────────────────────────────

def run_nightly_sweep():
    """Runs after US market close every weekday. Simulation on alpaca_paper, live on ibkr_live."""
    try:
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
            _send_discord_alert(
                f"💰 Sweep simulation: Would park "
                f"€{plan['sweepable_eur']:.0f} in {plan['sweep_ticker']}. "
                f"Est. daily yield: €{plan['est_daily_yield']:.2f}"
            )
    except Exception as e:
        log_event("ERROR", "nightly_sweep_failed", {"error": str(e)[:100]})


# ── Weekly digest (called by scheduler) ──────────────────────────────────────

def run_weekly_digest():
    from database.client import get_recent_trades, save_learning
    trades = get_recent_trades(days=7)
    if not trades:
        return
    insights = generate_weekly_insights(trades)
    from datetime import date
    save_learning(
        week_start      = date.today(),
        insights        = insights,
        trades_analysed = len(trades)
    )
    log_event("LEARNING", "weekly_digest", {"insights": len(insights)})
    return insights


# ── Scheduler entry point ─────────────────────────────────────────────────────

def start_scheduler():
    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler(timezone=NY_TZ or timezone.utc)

    # Main signal cycle: every 5 minutes during regular US market hours.
    # Scheduling in New York time keeps the window correct across DST changes.
    if NY_TZ is not None:
        scheduler.add_job(run_signal_cycle, "cron",
                          day_of_week="mon-fri",
                          hour=9,
                          minute="30-59/5")
        scheduler.add_job(run_signal_cycle, "cron",
                          day_of_week="mon-fri",
                          hour="10-15",
                          minute="*/5")
    else:
        scheduler.add_job(_run_signal_cycle_if_market_open, "cron",
                          day_of_week="mon-fri",
                          hour="13-21",
                          minute="*/5")

    # Weekly digest: Sunday evening
    scheduler.add_job(run_weekly_digest, "cron",
                      day_of_week="sun", hour=18, minute=0,
                      timezone=timezone.utc)

    # Nightly cash sweep: after US market close Mon-Fri
    scheduler.add_job(run_nightly_sweep, "cron",
                      day_of_week="mon-fri", hour=16, minute=5)

    log_event("INFO", "scheduler_started", {"tickers": TICKERS, "profile": PROFILE.get("_name")})
    print(f"Agent started | Profile: {PROFILE['display_name']} | Tickers: {TICKERS}")
    scheduler.start()


if __name__ == "__main__":
    mode = os.getenv("AGENT_MODE", "scheduler")
    if mode == "sweep":
        run_nightly_sweep()
    elif mode == "digest":
        run_weekly_digest()
    elif mode == "signal":
        run_signal_cycle()
    elif mode == "swing_reeval":
        re_evaluate_swing_positions()
    else:
        start_scheduler()
