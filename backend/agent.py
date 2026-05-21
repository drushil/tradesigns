"""
backend/agent.py
Main agent loop. Runs on a schedule, ties together:
signals → risk gate → EV check → LLM decision → execution → learning → logging
"""
from __future__ import annotations
import os
import time
import asyncio
import math
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

try:
    import tomllib
except Exception:  # pragma: no cover - Python < 3.11 fallback
    try:
        import tomli as tomllib
    except Exception:
        tomllib = None

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
                                          detect_momentum_swing,
                                          prefetch_newsapi_batch)
from backend.broker.alpaca       import (get_account, get_positions, submit_market_order,
                                          close_position, close_partial_position,
                                          pre_trade_gate, compute_position_size,
                                          scan_for_extreme_dips, get_order_by_id,
                                          cancel_order_by_id, submit_stop_order,
                                          cancel_open_orders_for_symbol)
from backend.learning.engine     import (RegimeAwareWeightEngine, attribute_signals,
                                          compute_expected_value, get_effective_profile,
                                          generate_weekly_insights, llm_signal_decision,
                                          build_weight_engine_from_trades)
from database.client             import (insert_trade, insert_signal, get_recent_trades,
                                          save_signal_weights, get_latest_weights,
                                          save_snapshot, save_learning, log_event, get_logs,
                                          save_open_trade, get_open_trade_records,
                                          close_open_trade_record,
                                          insert_blocked_opportunity,
                                          get_unchecked_blocked_opportunities,
                                          update_blocked_opportunity_replay,
                                          get_unchecked_closed_trades_for_replay,
                                          update_trade_post_exit_replay,
                                          update_signal,
                                          get_signal_percentiles,
                                          upsert_signal_percentiles)
from backend.grading.engine      import (grade_setup, compute_sector_confirmation,
                                          get_ticker_percentile_rank,
                                          merge_percentile_window,
                                          compute_percentile_thresholds,
                                          grade_sort_key, effective_size_multiplier,
                                          a_plus_hard_blocks, SetupGrade)
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


_DEFAULT_SECTOR_UNIVERSE = {
    "defaults": {
        "core_tickers": ["SPY", "QQQ", "GLD", "TLT", "SMH", "NVDA", "AMD", "META"],
        "index_or_etf_tickers": [
            "SPY", "QQQ", "IWM", "DIA", "GLD", "TLT", "IEF", "SHY", "SGOV", "BIL",
            "SMH", "XOP", "XLE", "XLF", "XLV", "VGT", "IBIT", "TQQQ", "SOXL", "NVDL",
        ],
        "defensive_tickers": ["GLD", "TLT", "IEF", "SHY", "SGOV", "BIL"],
        "inverse_etfs": ["SH", "PSQ", "SQQQ", "SPXU", "SDS", "QID", "DOG", "TZA"],
    },
    "sectors": {
        "semis": {
            "proxy": "SMH",
            "core": ["NVDA", "AMD", "ARM", "AVGO", "SMH", "MU"],
            "shadow": ["TSM", "ASML", "INTC", "QCOM"],
            "leveraged": ["SOXL", "NVDL"],
            "max_live_per_cycle": 2,
            "max_leveraged_per_cycle": 0,
            "leadership_bonus": 0.15,
            "min_5d_return_for_bonus_pct": 2.0,
        },
        "broad_tech": {
            "proxy": "QQQ",
            "core": ["QQQ", "META", "AMZN", "AAPL", "MSFT", "GOOGL", "PLTR"],
            "shadow": ["TSLA", "CRM", "NOW", "ORCL", "ADBE"],
            "max_live_per_cycle": 2,
            "max_leveraged_per_cycle": 0,
            "leadership_bonus": 0.10,
            "min_5d_return_for_bonus_pct": 2.0,
        },
        "ai_power": {
            "proxy": "XLI",
            "proxy_basket": ["VRT", "ETN", "CEG", "VST"],
            "core": ["VRT", "ETN", "CEG", "VST"],
            "shadow": ["GEV", "NEE", "PEG"],
            "max_live_per_cycle": 1,
            "max_leveraged_per_cycle": 0,
            "leadership_bonus": 0.12,
            "min_5d_return_for_bonus_pct": 1.5,
        },
        "crypto": {
            "proxy": "IBIT",
            "core": ["IBIT", "COIN", "MSTR"],
            "shadow": ["MARA", "RIOT"],
            "max_live_per_cycle": 1,
            "max_leveraged_per_cycle": 0,
            "leadership_bonus": 0.10,
            "min_5d_return_for_bonus_pct": 2.5,
        },
        "energy": {
            "proxy": "XOP",
            "core": ["XOP"],
            "shadow": ["XLE", "CVX", "XOM", "OXY"],
            "max_live_per_cycle": 1,
            "max_leveraged_per_cycle": 0,
            "leadership_bonus": 0.08,
            "min_5d_return_for_bonus_pct": 1.5,
        },
        "financials": {
            "proxy": "XLF",
            "core": ["XLF"],
            "shadow": ["JPM", "BAC", "GS", "MS"],
            "max_live_per_cycle": 1,
            "max_leveraged_per_cycle": 0,
            "leadership_bonus": 0.08,
            "min_5d_return_for_bonus_pct": 1.5,
        },
        "defensive": {
            "proxy": "TLT",
            "core": ["GLD", "TLT", "IEF", "SGOV"],
            "shadow": ["SHY", "BIL"],
            "max_live_per_cycle": 1,
            "max_leveraged_per_cycle": 0,
            "leadership_bonus": 0.05,
            "min_5d_return_for_bonus_pct": 1.0,
        },
        "broad_market": {
            "proxy": "SPY",
            "core": ["SPY", "IWM", "DIA"],
            "shadow": [],
            "max_live_per_cycle": 2,
            "max_leveraged_per_cycle": 0,
            "leadership_bonus": 0.05,
            "min_5d_return_for_bonus_pct": 1.0,
        },
    },
}
_SECTOR_CONFIG_WARNINGS: list[dict] = []
_logged_sector_config_warnings = False
_sector_return_cache: dict[tuple, tuple[datetime, dict[str, float]]] = {}


def _normalize_ticker_list(values) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        values = values.split(",")
    return [str(v).strip().upper() for v in values if str(v).strip()]


def _merge_sector_config(base: dict, override: dict) -> dict:
    def merge_dict(left: dict, right: dict) -> dict:
        merged = dict(left or {})
        for key, value in (right or {}).items():
            if isinstance(merged.get(key), dict) and isinstance(value, dict):
                merged[key] = merge_dict(merged[key], value)
            else:
                merged[key] = value
        return merged

    return merge_dict(base, override)


def _load_sector_universe_config() -> dict:
    config = _merge_sector_config(_DEFAULT_SECTOR_UNIVERSE, {})
    if tomllib is None:
        return config
    raw_path = _env_value("SECTOR_UNIVERSE_CONFIG_PATH", "config/sector_universe.toml")
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    if not path.exists():
        return config
    try:
        with path.open("rb") as fh:
            override = tomllib.load(fh)
        return _merge_sector_config(config, override)
    except Exception:
        return config


def _active_sector_names(config: dict) -> set[str]:
    configured = _normalize_ticker_list(os.getenv("ACTIVE_SECTORS", ""))
    if configured:
        available = {str(name).lower() for name in config.get("sectors", {})}
        requested = {s.lower() for s in configured}
        unknown = sorted(requested - available)
        if unknown:
            _SECTOR_CONFIG_WARNINGS.append({
                "warning": "unknown_active_sectors_ignored",
                "unknown": unknown,
                "available": sorted(available),
            })
        return requested & available
    return {
        str(name).lower()
        for name, sector in config.get("sectors", {}).items()
        if bool(sector.get("enabled", True))
    }


def _sector_members(sector: dict) -> set[str]:
    members = set()
    for key in ("core", "shadow", "leveraged", "aliases"):
        members.update(_normalize_ticker_list(sector.get(key, [])))
    return members


_SECTOR_UNIVERSE = _load_sector_universe_config()
_ACTIVE_SECTORS = _active_sector_names(_SECTOR_UNIVERSE)


def _sector_data(theme: str) -> dict:
    return dict((_SECTOR_UNIVERSE.get("sectors") or {}).get(str(theme or "").lower(), {}))


def _sector_setting(theme: str, key: str, default=None):
    return _sector_data(theme).get(key, default)


def _sector_default_tickers(key: str) -> set[str]:
    return set(_normalize_ticker_list((_SECTOR_UNIVERSE.get("defaults") or {}).get(key, [])))


_THEME_MAP = {
    name: _sector_members(sector)
    for name, sector in (_SECTOR_UNIVERSE.get("sectors") or {}).items()
    if str(name).lower() in _ACTIVE_SECTORS
}
_THEME_PROXIES = {
    name: str(sector.get("proxy", "")).strip().upper()
    for name, sector in (_SECTOR_UNIVERSE.get("sectors") or {}).items()
    if str(name).lower() in _ACTIVE_SECTORS and str(sector.get("proxy", "")).strip()
}
_THEME_PROXY_BASKETS = {
    name: _normalize_ticker_list(sector.get("proxy_basket", []))
    for name, sector in (_SECTOR_UNIVERSE.get("sectors") or {}).items()
    if str(name).lower() in _ACTIVE_SECTORS and _normalize_ticker_list(sector.get("proxy_basket", []))
}
_DYNAMIC_CANDIDATE_POOL = {
    name: _normalize_ticker_list(
        list(sector.get("core", [])) + list(sector.get("leveraged", [])) + list(sector.get("shadow", []))
    )
    for name, sector in (_SECTOR_UNIVERSE.get("sectors") or {}).items()
    if str(name).lower() in _ACTIVE_SECTORS
}
_DEFAULT_CORE_TICKERS = _sector_default_tickers("core_tickers")
_CONFIG_LEVERAGED_TICKERS = {
    ticker
    for sector in (_SECTOR_UNIVERSE.get("sectors") or {}).values()
    for ticker in _normalize_ticker_list(sector.get("leveraged", []))
}


def _default_ticker_universe() -> str:
    core = []
    for sector_name, sector in (_SECTOR_UNIVERSE.get("sectors") or {}).items():
        if str(sector_name).lower() not in _ACTIVE_SECTORS:
            continue
        core.extend(_normalize_ticker_list(sector.get("core", [])))
    if core:
        return ",".join(dict.fromkeys(core))
    return "SPY,QQQ,GLD"


TICKERS  = [t.strip().upper() for t in _env_value("TICKER_UNIVERSE", _default_ticker_universe()).split(",") if t.strip()]
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

# Composites collected across all tickers this cycle — used for sector confirmation
_cycle_composites: dict[str, float] = {}

# Percentile baseline from DB — loaded once per cycle
_cycle_db_percentiles: dict[str, dict] = {}

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


def _minutes_to_regular_close(now: datetime = None) -> Optional[int]:
    """Minutes until regular US market close, or None outside regular hours."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if not is_regular_us_market_hours(now):
        return None
    ny_now = _to_new_york_time(now)
    close = _env_int("EOD_CLEANUP_NY_CLOSE_HOUR", 16) * 60 + _env_int("EOD_CLEANUP_NY_CLOSE_MINUTE", 0)
    current = ny_now.hour * 60 + ny_now.minute
    return max(0, close - current)


def _minutes_since_regular_open(now: datetime = None) -> Optional[int]:
    """Minutes since regular US market open, or None outside regular market hours."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if not is_regular_us_market_hours(now):
        return None
    ny_now = _to_new_york_time(now)
    open_minutes = 9 * 60 + 30
    current = ny_now.hour * 60 + ny_now.minute
    return max(0, current - open_minutes)


def _is_eod_final_force_exit_window(now: datetime = None) -> bool:
    """Return True in the final EOD window where intraday positions must close."""
    minutes_to_close = _minutes_to_regular_close(now)
    if minutes_to_close is None:
        return False
    return minutes_to_close <= _env_int("EOD_FINAL_FORCE_EXIT_MINUTES", 5)


def _is_new_intraday_entry_too_late(ticker: str, now: datetime = None) -> Optional[dict]:
    """Block fresh intraday entries near close unless the ticker is swing-eligible."""
    minutes_to_close = _minutes_to_regular_close(now)
    if minutes_to_close is None:
        return None
    buffer_minutes = _env_int("EOD_NEW_ENTRY_BLOCK_MINUTES", 25)
    ticker = str(ticker or "").upper()
    if minutes_to_close < buffer_minutes and ticker not in SWING_TICKERS:
        return {
            "ticker": ticker,
            "minutes_to_close": minutes_to_close,
            "buffer_minutes": buffer_minutes,
            "reason": "eod_new_intraday_entry_block",
        }
    return None


def _is_leveraged_etf(ticker: str, profile: dict) -> bool:
    """Return True if ticker is a leveraged ETF defined in the profile."""
    if not profile.get("allow_leveraged_etfs"):
        return False
    configured = {t.upper() for t in profile.get("leveraged_etf_tickers", [])}
    configured.update(_CONFIG_LEVERAGED_TICKERS)
    return ticker.upper() in configured


def _leveraged_etf_stop_scalar(ticker: str, profile: dict) -> float:
    """Extra stop room for leveraged ETFs, where normal ATR stops are often too tight."""
    if not _is_leveraged_etf(ticker, profile):
        return 1.0
    return max(1.0, float(profile.get("leveraged_etf_stop_scalar", 1.35)))


def _leveraged_etf_max_hold_window(now: datetime = None) -> bool:
    """Return True if we are past the leveraged ETF max-hold cutoff (default 3:45 PM ET)."""
    now = now or datetime.now(timezone.utc)
    ny_now = _to_new_york_time(now)
    cutoff_minutes = 15 * 60 + 45  # 3:45 PM default
    return (ny_now.hour * 60 + ny_now.minute) >= cutoff_minutes


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
    fx_rate   = float(os.getenv("EURUSD_RATE", "1.08") or "1.08")
    unrealized_pl_usd = sum(float(p.get("unrealized_pl") or 0) for p in positions)
    net_market_value_usd = sum(float(p.get("market_value") or 0) for p in positions)
    gross_market_value_usd = sum(abs(float(p.get("market_value") or 0)) for p in positions)

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
    start_usd = start_eur * fx_rate
    drawdown  = max(0.0, (start_usd - equity) / start_usd * 100)

    return {
        "equity":       round(equity, 2),
        "cash":         round(cash, 2),
        "equity_eur":   round(equity / fx_rate, 2),
        "cash_eur":     round(cash / fx_rate, 2),
        "fx_rate":      fx_rate,
        "broker_equity_usd": account.get("alpaca_actual_usd"),
        "broker_cash_usd": account.get("alpaca_cash_usd"),
        "capital_ceiling_eur": account.get("capital_ceiling_eur"),
        "capital_ceiling_usd": account.get("capital_ceiling_usd"),
        "unrealized_pnl_usd": round(unrealized_pl_usd, 2),
        "unrealized_pnl_eur": round(unrealized_pl_usd / fx_rate, 2),
        "net_market_value_usd": round(net_market_value_usd, 2),
        "gross_market_value_usd": round(gross_market_value_usd, 2),
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
    p.setdefault("min_grade_required", "A")
    p.setdefault("allow_b_grade_exploration", False)
    p.setdefault("b_grade_size_multiplier", 0.20)
    p.setdefault("ev_reduced_size_floor_pct", -0.02)
    p.setdefault("ev_probe_floor_pct", -0.10)
    p.setdefault("ev_breakout_probe_min_quality", 0.65)
    p.setdefault("ev_reduced_size_multiplier", 0.65)
    p.setdefault("ev_probe_size_multiplier", 0.35)
    p.setdefault("allow_event_risk_intraday_probes", True)
    p.setdefault("event_risk_probe_min_score", 0.32)
    p.setdefault("event_risk_probe_min_macd", 0.35)
    p.setdefault("event_risk_probe_min_tape", 0.25)
    p.setdefault("event_risk_probe_min_relative_strength", 0.35)
    p.setdefault("event_risk_probe_size_multiplier", 0.30)
    p.setdefault("event_risk_max_hold_minutes", 30)
    p.setdefault("event_risk_stop_multiplier", 0.75)
    p.setdefault("event_risk_min_stop_pct", 0.25)
    p.setdefault("event_risk_latest_entry_utc_hour", 19)
    p.setdefault("max_new_intraday_trades_per_cycle", 2)
    p.setdefault("leveraged_etf_stop_scalar", 1.35)
    p.setdefault("a_plus_full_size_max_atr_pct", 2.5)
    p.setdefault("a_plus_full_size_max_stop_pct", 5.0)
    p.setdefault("grade_ev_override_negative_min_samples", 10)
    p.setdefault("probe_floor_inflation_max_multiple", 1.25)
    p.setdefault("ranging_regime_size_multiplier", 0.35)
    p.setdefault("ranging_max_trades_per_day", 6)
    p.setdefault("ranging_min_grade_required", "A+")
    p.setdefault("ranging_a_grade_min_breakout_quality", 0.80)
    p.setdefault("ranging_a_grade_min_ev_pct", 0.25)
    p.setdefault("ranging_a_plus_min_composite", 0.25)
    p.setdefault("ranging_a_plus_min_breakout_quality", 0.70)
    p.setdefault("ranging_a_plus_min_ev_pct", 0.20)
    p.setdefault("ranging_max_notional_eur", 1200)
    p.setdefault("ranging_leveraged_min_ev_pct", 0.25)
    p.setdefault("ranging_probe_enabled", True)
    p.setdefault("ranging_probe_allowed_grades", "A+,A")
    p.setdefault("ranging_probe_shadow_grades", "B")
    p.setdefault("ranging_probe_size_multiplier", 0.35)
    p.setdefault("ranging_probe_grade_b_shadow_size_multiplier", 0.20)
    p.setdefault("ranging_probe_grade_b_min_composite", 0.40)
    p.setdefault("ranging_probe_grade_b_min_breakout_quality", 0.60)
    p.setdefault("ranging_probe_grade_b_promote_min_samples", 8)
    p.setdefault("ranging_probe_grade_b_promote_min_win_rate", 0.55)
    p.setdefault("ranging_probe_grade_b_promote_requires_mfe_gt_mae", True)
    p.setdefault("ranging_probe_grade_b_promote_size_multiplier", 0.20)
    p.setdefault("ranging_probe_min_ev_pct", 0.03)
    p.setdefault("ranging_probe_min_composite", 0.20)
    p.setdefault("ranging_probe_min_breakout_quality", 0.35)
    p.setdefault("ranging_probe_min_aligned_signals", 2)
    p.setdefault("ranging_probe_min_macd", 0.10)
    p.setdefault("ranging_probe_min_tape", 0.10)
    p.setdefault("ranging_probe_min_relative_strength", 0.25)
    p.setdefault("ranging_probe_max_tape_against", 0.05)
    p.setdefault("ranging_probe_min_sector_relative_pct", -0.50)
    p.setdefault("ranging_probe_blocked_themes", "")
    p.setdefault("thesis_invalidated_cooldown_minutes", 75)
    p.setdefault("ranging_stop_loss_cooldown_minutes", 90)
    p.setdefault("min_reward_risk_ratio", 1.5)
    p.setdefault("ranging_min_reward_risk_ratio", 2.0)
    p.setdefault("signal_consensus_min_count", 3)
    p.setdefault("ranging_signal_consensus_min_count", 4)
    p.setdefault("ranging_core_consensus_min_count", 2)
    p.setdefault("ranging_core_consensus_enabled", True)
    p.setdefault("signal_consensus_min_strength", 0.15)
    p.setdefault("max_open_positions_per_theme", 2)
    p.setdefault("allow_a_plus_llm_hold_override", False)
    p.setdefault("alignment_orb_veto_threshold", 0.50)
    p.setdefault("alignment_tape_veto_threshold", 0.40)
    p.setdefault("alignment_put_call_veto_threshold", 0.50)
    p.setdefault("alignment_rsi_veto_threshold", 0.50)
    p.setdefault("sector_momentum_bonus_enabled", True)
    p.setdefault("sector_momentum_lookback_period", "5d")
    p.setdefault("sector_momentum_leadership_threshold_pct", 2.0)
    p.setdefault("sector_momentum_max_bonus", 0.15)
    p.setdefault("theme_max_candidates_per_cycle", 2)
    p.setdefault("theme_max_leveraged_candidates_per_cycle", 1)
    p.setdefault("dynamic_universe_shadow_enabled", True)
    p.setdefault("dynamic_universe_max_shadow_per_theme", 2)
    if IS_PAPER_TRADING:
        for key, value in p.get("paper_overrides", {}).items():
            p[key] = value
    if os.getenv("MIN_GRADE_REQUIRED"):
        p["min_grade_required"] = os.getenv("MIN_GRADE_REQUIRED", "").strip().upper()
    if os.getenv("ALLOW_B_GRADE_EXPLORATION") is not None:
        p["allow_b_grade_exploration"] = _env_bool("ALLOW_B_GRADE_EXPLORATION", False)
    if os.getenv("B_GRADE_SIZE_MULTIPLIER"):
        p["b_grade_size_multiplier"] = _env_float("B_GRADE_SIZE_MULTIPLIER", p["b_grade_size_multiplier"])
    if os.getenv("GRADE_EV_OVERRIDE_NEGATIVE_MIN_SAMPLES"):
        p["grade_ev_override_negative_min_samples"] = _env_int(
            "GRADE_EV_OVERRIDE_NEGATIVE_MIN_SAMPLES",
            int(p["grade_ev_override_negative_min_samples"]),
        )
    if os.getenv("PROBE_FLOOR_INFLATION_MAX_MULTIPLE"):
        p["probe_floor_inflation_max_multiple"] = _env_float(
            "PROBE_FLOOR_INFLATION_MAX_MULTIPLE",
            p["probe_floor_inflation_max_multiple"],
        )
    if os.getenv("RANGING_MAX_TRADES_PER_DAY"):
        p["ranging_max_trades_per_day"] = _env_int("RANGING_MAX_TRADES_PER_DAY", int(p["ranging_max_trades_per_day"]))
    if os.getenv("RANGING_REGIME_SIZE_MULTIPLIER"):
        p["ranging_regime_size_multiplier"] = _env_float("RANGING_REGIME_SIZE_MULTIPLIER", p["ranging_regime_size_multiplier"])
    if os.getenv("RANGING_A_PLUS_MIN_COMPOSITE"):
        p["ranging_a_plus_min_composite"] = _env_float("RANGING_A_PLUS_MIN_COMPOSITE", p["ranging_a_plus_min_composite"])
    if os.getenv("RANGING_A_PLUS_MIN_BREAKOUT_QUALITY"):
        p["ranging_a_plus_min_breakout_quality"] = _env_float(
            "RANGING_A_PLUS_MIN_BREAKOUT_QUALITY",
            p["ranging_a_plus_min_breakout_quality"],
        )
    if os.getenv("RANGING_A_PLUS_MIN_EV_PCT"):
        p["ranging_a_plus_min_ev_pct"] = _env_float("RANGING_A_PLUS_MIN_EV_PCT", p["ranging_a_plus_min_ev_pct"])
    if os.getenv("RANGING_A_GRADE_MIN_BREAKOUT_QUALITY"):
        p["ranging_a_grade_min_breakout_quality"] = _env_float(
            "RANGING_A_GRADE_MIN_BREAKOUT_QUALITY",
            p["ranging_a_grade_min_breakout_quality"],
        )
    if os.getenv("RANGING_A_GRADE_MIN_EV_PCT"):
        p["ranging_a_grade_min_ev_pct"] = _env_float("RANGING_A_GRADE_MIN_EV_PCT", p["ranging_a_grade_min_ev_pct"])
    if os.getenv("RANGING_PROBE_ENABLED"):
        p["ranging_probe_enabled"] = _env_bool("RANGING_PROBE_ENABLED", bool(p["ranging_probe_enabled"]))
    if os.getenv("RANGING_PROBE_ALLOWED_GRADES"):
        p["ranging_probe_allowed_grades"] = _env_value(
            "RANGING_PROBE_ALLOWED_GRADES",
            str(p["ranging_probe_allowed_grades"]),
        )
    if os.getenv("RANGING_PROBE_SHADOW_GRADES"):
        p["ranging_probe_shadow_grades"] = _env_value(
            "RANGING_PROBE_SHADOW_GRADES",
            str(p["ranging_probe_shadow_grades"]),
        )
    if os.getenv("RANGING_PROBE_SIZE_MULTIPLIER"):
        p["ranging_probe_size_multiplier"] = _env_float(
            "RANGING_PROBE_SIZE_MULTIPLIER",
            p["ranging_probe_size_multiplier"],
        )
    if os.getenv("RANGING_PROBE_GRADE_B_SHADOW_SIZE_MULTIPLIER"):
        p["ranging_probe_grade_b_shadow_size_multiplier"] = _env_float(
            "RANGING_PROBE_GRADE_B_SHADOW_SIZE_MULTIPLIER",
            p["ranging_probe_grade_b_shadow_size_multiplier"],
        )
    if os.getenv("RANGING_PROBE_GRADE_B_MIN_COMPOSITE"):
        p["ranging_probe_grade_b_min_composite"] = _env_float(
            "RANGING_PROBE_GRADE_B_MIN_COMPOSITE",
            p["ranging_probe_grade_b_min_composite"],
        )
    if os.getenv("RANGING_PROBE_GRADE_B_MIN_BREAKOUT_QUALITY"):
        p["ranging_probe_grade_b_min_breakout_quality"] = _env_float(
            "RANGING_PROBE_GRADE_B_MIN_BREAKOUT_QUALITY",
            p["ranging_probe_grade_b_min_breakout_quality"],
        )
    if os.getenv("RANGING_PROBE_GRADE_B_PROMOTE_MIN_SAMPLES"):
        p["ranging_probe_grade_b_promote_min_samples"] = _env_int(
            "RANGING_PROBE_GRADE_B_PROMOTE_MIN_SAMPLES",
            int(p["ranging_probe_grade_b_promote_min_samples"]),
        )
    if os.getenv("RANGING_PROBE_GRADE_B_PROMOTE_MIN_WIN_RATE"):
        p["ranging_probe_grade_b_promote_min_win_rate"] = _env_float(
            "RANGING_PROBE_GRADE_B_PROMOTE_MIN_WIN_RATE",
            p["ranging_probe_grade_b_promote_min_win_rate"],
        )
    if os.getenv("RANGING_PROBE_GRADE_B_PROMOTE_REQUIRES_MFE_GT_MAE"):
        p["ranging_probe_grade_b_promote_requires_mfe_gt_mae"] = _env_bool(
            "RANGING_PROBE_GRADE_B_PROMOTE_REQUIRES_MFE_GT_MAE",
            bool(p["ranging_probe_grade_b_promote_requires_mfe_gt_mae"]),
        )
    if os.getenv("RANGING_PROBE_GRADE_B_PROMOTE_SIZE_MULTIPLIER"):
        p["ranging_probe_grade_b_promote_size_multiplier"] = _env_float(
            "RANGING_PROBE_GRADE_B_PROMOTE_SIZE_MULTIPLIER",
            p["ranging_probe_grade_b_promote_size_multiplier"],
        )
    if os.getenv("RANGING_PROBE_MIN_EV_PCT"):
        p["ranging_probe_min_ev_pct"] = _env_float("RANGING_PROBE_MIN_EV_PCT", p["ranging_probe_min_ev_pct"])
    if os.getenv("RANGING_PROBE_MIN_COMPOSITE"):
        p["ranging_probe_min_composite"] = _env_float(
            "RANGING_PROBE_MIN_COMPOSITE",
            p["ranging_probe_min_composite"],
        )
    if os.getenv("RANGING_PROBE_MIN_BREAKOUT_QUALITY"):
        p["ranging_probe_min_breakout_quality"] = _env_float(
            "RANGING_PROBE_MIN_BREAKOUT_QUALITY",
            p["ranging_probe_min_breakout_quality"],
        )
    if os.getenv("RANGING_PROBE_MIN_ALIGNED_SIGNALS"):
        p["ranging_probe_min_aligned_signals"] = _env_int(
            "RANGING_PROBE_MIN_ALIGNED_SIGNALS",
            int(p["ranging_probe_min_aligned_signals"]),
        )
    if os.getenv("RANGING_PROBE_MIN_MACD"):
        p["ranging_probe_min_macd"] = _env_float("RANGING_PROBE_MIN_MACD", p["ranging_probe_min_macd"])
    if os.getenv("RANGING_PROBE_MIN_TAPE"):
        p["ranging_probe_min_tape"] = _env_float("RANGING_PROBE_MIN_TAPE", p["ranging_probe_min_tape"])
    if os.getenv("RANGING_PROBE_MIN_RELATIVE_STRENGTH"):
        p["ranging_probe_min_relative_strength"] = _env_float(
            "RANGING_PROBE_MIN_RELATIVE_STRENGTH",
            p["ranging_probe_min_relative_strength"],
        )
    if os.getenv("RANGING_PROBE_MAX_TAPE_AGAINST"):
        p["ranging_probe_max_tape_against"] = _env_float(
            "RANGING_PROBE_MAX_TAPE_AGAINST",
            p["ranging_probe_max_tape_against"],
        )
    if os.getenv("RANGING_PROBE_MIN_SECTOR_RELATIVE_PCT"):
        p["ranging_probe_min_sector_relative_pct"] = _env_float(
            "RANGING_PROBE_MIN_SECTOR_RELATIVE_PCT",
            p["ranging_probe_min_sector_relative_pct"],
        )
    if os.getenv("RANGING_PROBE_BLOCKED_THEMES"):
        p["ranging_probe_blocked_themes"] = _env_value(
            "RANGING_PROBE_BLOCKED_THEMES",
            str(p["ranging_probe_blocked_themes"]),
        )
    if os.getenv("RANGING_MAX_NOTIONAL_EUR"):
        p["ranging_max_notional_eur"] = _env_float("RANGING_MAX_NOTIONAL_EUR", p["ranging_max_notional_eur"])
    if os.getenv("THESIS_INVALIDATED_COOLDOWN_MINUTES"):
        p["thesis_invalidated_cooldown_minutes"] = _env_int(
            "THESIS_INVALIDATED_COOLDOWN_MINUTES",
            int(p["thesis_invalidated_cooldown_minutes"]),
        )
    if os.getenv("RANGING_STOP_LOSS_COOLDOWN_MINUTES"):
        p["ranging_stop_loss_cooldown_minutes"] = _env_int(
            "RANGING_STOP_LOSS_COOLDOWN_MINUTES",
            int(p["ranging_stop_loss_cooldown_minutes"]),
        )
    if os.getenv("MIN_REWARD_RISK_RATIO"):
        p["min_reward_risk_ratio"] = _env_float("MIN_REWARD_RISK_RATIO", p["min_reward_risk_ratio"])
    if os.getenv("RANGING_MIN_REWARD_RISK_RATIO"):
        p["ranging_min_reward_risk_ratio"] = _env_float(
            "RANGING_MIN_REWARD_RISK_RATIO",
            p["ranging_min_reward_risk_ratio"],
        )
    if os.getenv("SIGNAL_CONSENSUS_MIN_COUNT"):
        p["signal_consensus_min_count"] = _env_int(
            "SIGNAL_CONSENSUS_MIN_COUNT",
            int(p["signal_consensus_min_count"]),
        )
    if os.getenv("RANGING_SIGNAL_CONSENSUS_MIN_COUNT"):
        p["ranging_signal_consensus_min_count"] = _env_int(
            "RANGING_SIGNAL_CONSENSUS_MIN_COUNT",
            int(p["ranging_signal_consensus_min_count"]),
        )
    if os.getenv("RANGING_CORE_CONSENSUS_ENABLED"):
        p["ranging_core_consensus_enabled"] = _env_bool(
            "RANGING_CORE_CONSENSUS_ENABLED",
            bool(p["ranging_core_consensus_enabled"]),
        )
    if os.getenv("RANGING_CORE_CONSENSUS_MIN_COUNT"):
        p["ranging_core_consensus_min_count"] = _env_int(
            "RANGING_CORE_CONSENSUS_MIN_COUNT",
            int(p["ranging_core_consensus_min_count"]),
        )
    if os.getenv("SIGNAL_CONSENSUS_MIN_STRENGTH"):
        p["signal_consensus_min_strength"] = _env_float(
            "SIGNAL_CONSENSUS_MIN_STRENGTH",
            p["signal_consensus_min_strength"],
        )
    if os.getenv("MAX_OPEN_POSITIONS_PER_THEME"):
        p["max_open_positions_per_theme"] = _env_int(
            "MAX_OPEN_POSITIONS_PER_THEME",
            int(p["max_open_positions_per_theme"]),
        )
    if os.getenv("SECTOR_MOMENTUM_BONUS_ENABLED") is not None:
        p["sector_momentum_bonus_enabled"] = _env_bool("SECTOR_MOMENTUM_BONUS_ENABLED", True)
    if os.getenv("SECTOR_MOMENTUM_LEADERSHIP_THRESHOLD_PCT"):
        p["sector_momentum_leadership_threshold_pct"] = _env_float(
            "SECTOR_MOMENTUM_LEADERSHIP_THRESHOLD_PCT",
            p["sector_momentum_leadership_threshold_pct"],
        )
    if os.getenv("SECTOR_MOMENTUM_MAX_BONUS"):
        p["sector_momentum_max_bonus"] = _env_float("SECTOR_MOMENTUM_MAX_BONUS", p["sector_momentum_max_bonus"])
    if os.getenv("THEME_MAX_CANDIDATES_PER_CYCLE"):
        p["theme_max_candidates_per_cycle"] = _env_int(
            "THEME_MAX_CANDIDATES_PER_CYCLE",
            int(p["theme_max_candidates_per_cycle"]),
        )
    if os.getenv("THEME_MAX_LEVERAGED_CANDIDATES_PER_CYCLE"):
        p["theme_max_leveraged_candidates_per_cycle"] = _env_int(
            "THEME_MAX_LEVERAGED_CANDIDATES_PER_CYCLE",
            int(p["theme_max_leveraged_candidates_per_cycle"]),
        )
    if os.getenv("DYNAMIC_UNIVERSE_SHADOW_ENABLED") is not None:
        p["dynamic_universe_shadow_enabled"] = _env_bool("DYNAMIC_UNIVERSE_SHADOW_ENABLED", True)
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
_DEFENSIVE_TICKERS = _sector_default_tickers("defensive_tickers") or {"GLD", "TLT", "IEF", "SHY", "SGOV", "BIL"}
_INVERSE_ETFS = _sector_default_tickers("inverse_etfs") or _INVERSE_ETFS
_INDEX_OR_ETF_TICKERS = (
    _sector_default_tickers("index_or_etf_tickers")
    | _INVERSE_ETFS
    | _DEFENSIVE_TICKERS
    | _CONFIG_LEVERAGED_TICKERS
)
_PROBE_EV_DECISIONS = {
    "probe_size", "event_probe_size", "grade_ev_override_probe", "a_plus_probe",
    "b_grade_exploration_size",
}


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


def _ticker_theme(ticker: str) -> str:
    ticker = str(ticker or "").upper()
    for theme, members in _THEME_MAP.items():
        if ticker in members:
            return theme
    return "other"


def _return_pct_from_bars(ticker: str, period: str = "5d", interval: str = "1d") -> Optional[float]:
    return _return_pcts_from_bars([ticker], period=period, interval=interval).get(str(ticker or "").upper())


def _extract_close_series(downloaded, ticker: str):
    ticker = str(ticker or "").upper()
    if downloaded is None or downloaded.empty:
        return None
    columns = getattr(downloaded, "columns", None)
    if getattr(columns, "nlevels", 1) > 1:
        if ticker in columns.get_level_values(0):
            frame = downloaded[ticker]
            return frame["Close"].dropna() if "Close" in frame else None
        if "Close" in columns.get_level_values(0):
            close = downloaded["Close"]
            return close[ticker].dropna() if ticker in close else None
        return None
    if "Close" not in downloaded:
        return None
    return downloaded["Close"].dropna()


def _return_pcts_from_bars(tickers: list[str], period: str = "5d", interval: str = "1d") -> dict[str, float]:
    symbols = sorted(set(_normalize_ticker_list(tickers)))
    if not symbols:
        return {}
    now = datetime.now(timezone.utc)
    cache_key = (tuple(symbols), period, interval)
    cached = _sector_return_cache.get(cache_key)
    if cached and (now - cached[0]).total_seconds() < _env_int("SECTOR_MOMENTUM_CACHE_SECONDS", 900):
        return dict(cached[1])
    try:
        import yfinance as yf
        target = symbols[0] if len(symbols) == 1 else symbols
        bars = yf.download(
            target,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=True,
            group_by="ticker",
            threads=True,
        )
        results = {}
        for symbol in symbols:
            close = _extract_close_series(bars, symbol)
            if close is None or len(close) < 2:
                continue
            first = float(close.iloc[0])
            last = float(close.iloc[-1])
            if first <= 0:
                continue
            results[symbol] = (last - first) / first * 100
        _sector_return_cache[cache_key] = (now, results)
        return results
    except Exception:
        return {}


def _sector_proxy_symbols(theme: str, proxy: str) -> list[str]:
    basket = _THEME_PROXY_BASKETS.get(theme) or []
    return basket or [proxy]


def _sector_proxy_return(theme: str, proxy: str, returns: dict[str, float]) -> Optional[float]:
    symbols = _sector_proxy_symbols(theme, proxy)
    values = [returns[s] for s in symbols if s in returns]
    if not values:
        return None
    return sum(values) / len(values)


def _sector_momentum_snapshot(tickers: list[str], profile: dict) -> dict:
    if not _env_bool("SECTOR_MOMENTUM_BONUS_ENABLED", bool(profile.get("sector_momentum_bonus_enabled", True))):
        return {"enabled": False, "themes": {}, "ticker_multipliers": {}}
    lookback = _env_value("SECTOR_MOMENTUM_LOOKBACK_PERIOD", str(profile.get("sector_momentum_lookback_period", "5d")))
    leadership_threshold = _env_float(
        "SECTOR_MOMENTUM_LEADERSHIP_THRESHOLD_PCT",
        float(profile.get("sector_momentum_leadership_threshold_pct", 2.0)),
    )
    max_bonus = _env_float("SECTOR_MOMENTUM_MAX_BONUS", float(profile.get("sector_momentum_max_bonus", 0.15)))
    proxy_symbols = ["SPY"]
    for theme, proxy in _THEME_PROXIES.items():
        proxy_symbols.extend(_sector_proxy_symbols(theme, proxy))
    proxy_returns = _return_pcts_from_bars(proxy_symbols, period=lookback, interval="1d")
    spy_return = proxy_returns.get("SPY")
    themes = {}
    ticker_multipliers = {}
    if spy_return is None:
        return {"enabled": True, "spy_return_pct": None, "themes": {}, "ticker_multipliers": {}}
    for theme, proxy in _THEME_PROXIES.items():
        theme_return = _sector_proxy_return(theme, proxy, proxy_returns)
        if theme_return is None:
            continue
        relative = theme_return - spy_return
        theme_threshold = float(_sector_setting(theme, "min_5d_return_for_bonus_pct", leadership_threshold))
        theme_max_bonus = float(_sector_setting(theme, "leadership_bonus", max_bonus))
        theme_max_bonus = min(max_bonus, max(0.0, theme_max_bonus))
        leader = relative >= theme_threshold
        bonus = min(theme_max_bonus, max(0.0, relative / 20.0)) if leader else 0.0
        multiplier = round(1.0 + bonus, 4)
        themes[theme] = {
            "proxy": proxy,
            "proxy_basket": _sector_proxy_symbols(theme, proxy),
            "return_pct": round(theme_return, 3),
            "spy_return_pct": round(spy_return, 3),
            "relative_pct": round(relative, 3),
            "leader": leader,
            "multiplier": multiplier,
            "leadership_threshold_pct": theme_threshold,
        }
        if leader:
            for ticker in _THEME_MAP.get(theme, set()):
                if ticker in tickers:
                    ticker_multipliers[ticker] = multiplier
    return {
        "enabled": True,
        "lookback": lookback,
        "spy_return_pct": round(spy_return, 3),
        "themes": themes,
        "ticker_multipliers": ticker_multipliers,
    }


def _apply_sector_momentum_to_candidate(candidate: dict, momentum: dict) -> dict:
    ticker = str(candidate.get("ticker") or "").upper()
    theme = _ticker_theme(ticker)
    multiplier = float((momentum or {}).get("ticker_multipliers", {}).get(ticker, 1.0))
    setup_context = candidate.setdefault("setup_context", {})
    base_rank = float(setup_context.get("candidate_rank_score") or 0)
    setup_context["theme"] = theme
    setup_context["sector_momentum_multiplier"] = round(multiplier, 4)
    setup_context["base_candidate_rank_score"] = round(base_rank, 4)
    setup_context["sector_momentum"] = (momentum or {}).get("themes", {}).get(theme, {})
    if multiplier > 1.0:
        setup_context["candidate_rank_score"] = round(base_rank * multiplier, 4)
    return candidate


def _dynamic_universe_shadow_recommendations(tickers: list[str], momentum: dict,
                                             max_per_theme: int = 2) -> dict:
    existing = {str(t or "").upper() for t in tickers}
    recs = []
    for theme, data in (momentum or {}).get("themes", {}).items():
        if not data.get("leader"):
            continue
        added = 0
        for ticker in _DYNAMIC_CANDIDATE_POOL.get(theme, []):
            ticker = ticker.upper()
            if ticker in existing:
                continue
            recs.append({
                "ticker": ticker,
                "theme": theme,
                "reason": (
                    f"{theme} leading SPY by {data.get('relative_pct')}% "
                    f"over {momentum.get('lookback', '5d')}"
                ),
                "proxy": data.get("proxy"),
                "theme_relative_pct": data.get("relative_pct"),
                "mode": "shadow_only",
                "execution_allowed": False,
            })
            added += 1
            if added >= max_per_theme:
                break
    return {
        "core_tickers": sorted(existing),
        "configured_core_tickers": sorted(existing & _DEFAULT_CORE_TICKERS),
        "daily_intraday_tickers": [],
        "weekly_swing_tickers": [],
        "advisory_tickers": [],
        "shadow_candidates": recs,
        "execution_allowed": False,
    }


def _shadow_candidate_repeat_counts(candidates: list[dict], limit: int = 250) -> dict[str, int]:
    symbols = {str(c.get("ticker") or "").upper() for c in candidates}
    symbols.discard("")
    if not symbols:
        return {}
    counts = {symbol: 1 for symbol in symbols}
    try:
        for row in get_logs(level="INFO", limit=limit):
            if row.get("event") != "dynamic_universe_shadow_recommendations":
                continue
            detail = row.get("detail") or {}
            for candidate in detail.get("shadow_candidates") or []:
                ticker = str(candidate.get("ticker") or "").upper()
                if ticker in counts:
                    counts[ticker] += 1
        return counts
    except Exception:
        return counts


def _enrich_shadow_recommendation_repeats(payload: dict) -> dict:
    candidates = payload.get("shadow_candidates") or []
    counts = _shadow_candidate_repeat_counts(candidates)
    for candidate in candidates:
        ticker = str(candidate.get("ticker") or "").upper()
        candidate["recent_shadow_mentions"] = counts.get(ticker, 1)
    threshold = _env_int("DYNAMIC_UNIVERSE_REPEAT_REVIEW_THRESHOLD", 3)
    payload["repeat_review_candidates"] = [
        {
            "ticker": c.get("ticker"),
            "theme": c.get("theme"),
            "recent_shadow_mentions": c.get("recent_shadow_mentions", 1),
        }
        for c in candidates
        if int(c.get("recent_shadow_mentions") or 1) >= threshold
    ]
    payload["repeat_review_threshold"] = threshold
    return payload


def _log_dynamic_universe_shadow(tickers: list[str], momentum: dict, profile: dict):
    if not _env_bool(
        "DYNAMIC_UNIVERSE_SHADOW_ENABLED",
        bool(profile.get("dynamic_universe_shadow_enabled", True)),
    ):
        return
    max_per_theme = _env_int(
        "DYNAMIC_UNIVERSE_MAX_SHADOW_PER_THEME",
        int(profile.get("dynamic_universe_max_shadow_per_theme", 2)),
    )
    payload = _dynamic_universe_shadow_recommendations(tickers, momentum, max_per_theme=max_per_theme)
    if payload.get("shadow_candidates"):
        payload = _enrich_shadow_recommendation_repeats(payload)
        log_event("INFO", "dynamic_universe_shadow_recommendations", payload)


def _theme_cap_candidates(candidates: list[dict], profile: dict) -> tuple[list[dict], list[dict]]:
    max_per_theme = _env_int("THEME_MAX_CANDIDATES_PER_CYCLE", int(profile.get("theme_max_candidates_per_cycle", 2)))
    max_leveraged = _env_int(
        "THEME_MAX_LEVERAGED_CANDIDATES_PER_CYCLE",
        int(profile.get("theme_max_leveraged_candidates_per_cycle", 1)),
    )
    if max_per_theme <= 0:
        return list(candidates), []
    kept, skipped = [], []
    theme_counts: dict[str, int] = {}
    leveraged_counts: dict[str, int] = {}
    for candidate in candidates:
        ticker = candidate["ticker"]
        theme = candidate.get("setup_context", {}).get("theme") or _ticker_theme(ticker)
        is_leveraged = _is_leveraged_etf(ticker, profile)
        theme_max = int(_sector_setting(theme, "max_live_per_cycle", max_per_theme))
        theme_max_leveraged = int(_sector_setting(theme, "max_leveraged_per_cycle", max_leveraged))
        if theme_max <= 0:
            skipped.append({**candidate, "theme_cap_reason": "theme_disabled", "theme": theme})
            continue
        if theme_counts.get(theme, 0) >= theme_max:
            skipped.append({**candidate, "theme_cap_reason": "theme_candidate_cap", "theme": theme})
            continue
        if is_leveraged and leveraged_counts.get(theme, 0) >= theme_max_leveraged:
            skipped.append({**candidate, "theme_cap_reason": "theme_leveraged_cap", "theme": theme})
            continue
        theme_counts[theme] = theme_counts.get(theme, 0) + 1
        if is_leveraged:
            leveraged_counts[theme] = leveraged_counts.get(theme, 0) + 1
        kept.append(candidate)
    return kept, skipped


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


def _signal_score(signals: dict, name: str) -> float:
    try:
        return float((signals or {}).get(name, {}).get("score", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_probe_ev_decision(ev_decision: str) -> bool:
    return str(ev_decision or "") in _PROBE_EV_DECISIONS


def _alignment_veto(ticker: str, action: str, signals: dict, profile: dict) -> Optional[dict]:
    """Block obvious signal conflicts before LLM/execution can paper over them."""
    action = str(action or "").upper()
    direction = 1 if action == "BUY" else -1
    ticker = str(ticker or "").upper()
    orb = _signal_score(signals, "orb")
    tape = _signal_score(signals, "tape_aggression")
    put_call = _signal_score(signals, "put_call_ratio")
    rsi = _signal_score(signals, "rsi_divergence")
    macd = _signal_score(signals, "macd_crossover")
    rel = _signal_score(signals, "relative_strength")
    vwap = _signal_score(signals, "vwap_deviation")
    news = _signal_score(signals, "news_sentiment")

    orb_veto = float(profile.get("alignment_orb_veto_threshold", 0.50))
    tape_veto = float(profile.get("alignment_tape_veto_threshold", 0.40))
    options_veto = float(profile.get("alignment_put_call_veto_threshold", 0.50))
    rsi_veto = float(profile.get("alignment_rsi_veto_threshold", 0.50))

    checks = [
        ("orb", orb * direction, orb_veto),
        ("tape_aggression", tape * direction, tape_veto),
    ]
    for name, directional_score, threshold in checks:
        if directional_score < -abs(threshold):
            return {
                "reason": f"signal_alignment_veto_{name}",
                "signal": name,
                "score": round(directional_score, 4),
                "threshold": -abs(threshold),
            }

    if ticker in _INDEX_OR_ETF_TICKERS and put_call * direction < -abs(options_veto):
        return {
            "reason": "signal_alignment_veto_put_call_ratio",
            "signal": "put_call_ratio",
            "score": round(put_call * direction, 4),
            "threshold": -abs(options_veto),
        }

    bullish_confirmers = sum(
        1 for score in (tape, rel, vwap, orb, news)
        if score * direction > 0.15
    )
    if rsi * direction < -abs(rsi_veto) and macd * direction > 0.25 and bullish_confirmers <= 1:
        return {
            "reason": "signal_alignment_veto_rsi_macd_only",
            "signal": "rsi_divergence",
            "score": round(rsi * direction, 4),
            "macd": round(macd * direction, 4),
            "bullish_confirmers": bullish_confirmers,
        }
    return None


def _signal_consensus_block(action: str, signals: dict, regime: str,
                            profile: dict) -> Optional[dict]:
    """Require broad directional agreement instead of one loud signal."""
    direction = 1 if str(action or "").upper() == "BUY" else -1
    min_strength = abs(float(profile.get("signal_consensus_min_strength", 0.15)))
    min_count = int(profile.get("signal_consensus_min_count", 3))
    is_ranging = str(regime or "").lower() == "ranging"
    if is_ranging and bool(profile.get("ranging_core_consensus_enabled", True)):
        core_names = [
            "macd_crossover",
            "relative_strength",
            "tape_aggression",
            "vwap_deviation",
        ]
        aligned_core = []
        opposed_core = []
        observed_core = []
        for name in core_names:
            if name not in (signals or {}):
                continue
            directional_score = _signal_score(signals, name) * direction
            observed_core.append({"signal": name, "score": round(directional_score, 4)})
            if directional_score >= min_strength:
                aligned_core.append(name)
            elif directional_score <= -min_strength:
                opposed_core.append(name)

        core_min_count = int(profile.get("ranging_core_consensus_min_count", 2))
        if core_min_count <= 0 or len(aligned_core) >= core_min_count:
            return None
        return {
            "reason": "ranging_core_consensus_veto",
            "aligned_count": len(aligned_core),
            "min_count": core_min_count,
            "min_strength": min_strength,
            "aligned_signals": aligned_core,
            "opposed_signals": opposed_core,
            "observed_signals": observed_core,
            "core_signals": core_names,
        }

    if is_ranging:
        min_count = int(profile.get("ranging_signal_consensus_min_count", 4))
    if min_count <= 0:
        return None

    signal_names = [
        "rsi_divergence",
        "vwap_deviation",
        "news_sentiment",
        "tape_aggression",
        "order_book_imbalance",
        "orb",
        "macd_crossover",
        "relative_strength",
        "put_call_ratio",
    ]
    aligned = []
    opposed = []
    observed = []
    for name in signal_names:
        if name not in (signals or {}):
            continue
        directional_score = _signal_score(signals, name) * direction
        observed.append({"signal": name, "score": round(directional_score, 4)})
        if directional_score >= min_strength:
            aligned.append(name)
        elif directional_score <= -min_strength:
            opposed.append(name)

    if len(aligned) < min_count:
        return {
            "reason": "signal_consensus_veto",
            "aligned_count": len(aligned),
            "min_count": min_count,
            "min_strength": min_strength,
            "aligned_signals": aligned,
            "opposed_signals": opposed,
            "observed_signals": observed,
        }
    return None


def _reward_risk_block(stop_pct: float, take_profit_pct: float, regime: str,
                       profile: dict) -> Optional[dict]:
    """Block trades whose real order stop/target structure has poor payoff."""
    try:
        stop_pct = abs(float(stop_pct or 0))
        take_profit_pct = abs(float(take_profit_pct or 0))
    except (TypeError, ValueError):
        stop_pct = 0.0
        take_profit_pct = 0.0
    min_rr = float(profile.get("min_reward_risk_ratio", 1.5))
    if str(regime or "").lower() == "ranging":
        min_rr = float(profile.get("ranging_min_reward_risk_ratio", 2.0))
    if min_rr <= 0:
        return None
    rr = take_profit_pct / stop_pct if stop_pct > 0 else 0.0
    if stop_pct <= 0 or take_profit_pct <= 0 or rr < min_rr:
        return {
            "reason": "reward_risk_veto",
            "stop_pct": round(stop_pct, 4),
            "take_profit_pct": round(take_profit_pct, 4),
            "reward_risk": round(rr, 4),
            "min_reward_risk": min_rr,
        }
    return None


def _theme_open_exposure_block(ticker: str, profile: dict) -> Optional[dict]:
    max_open = int(profile.get("max_open_positions_per_theme", 2))
    if max_open <= 0:
        return None
    theme = _ticker_theme(ticker)
    open_same_theme = []
    for open_ticker, trade in (_open_trades or {}).items():
        if str(trade.get("status") or "open").lower() == "closed":
            continue
        if _ticker_theme(open_ticker) == theme:
            open_same_theme.append(str(open_ticker).upper())
    if len(open_same_theme) >= max_open:
        return {
            "reason": "theme_open_exposure_cap",
            "theme": theme,
            "open_theme_positions": sorted(open_same_theme),
            "max_open_positions_per_theme": max_open,
        }
    return None


def _csv_upper_set(value: str) -> set[str]:
    return {item.strip().upper() for item in str(value or "").split(",") if item.strip()}


def _ranging_probe_decision(ticker: str, setup_context: dict, ev_result: dict,
                            grade: str, profile: dict, block_reason: str,
                            signals_snap: dict = None) -> dict:
    """Decide whether a strict ranging-regime block should become a tiny probe."""
    setup_context = setup_context or {}
    ev_result = ev_result or {}
    signals_snap = signals_snap or {}
    grade = str(grade or "").upper()
    action = str(setup_context.get("action") or "BUY").upper()
    direction = 1 if action == "BUY" else -1
    allowed_grades = _csv_upper_set(profile.get("ranging_probe_allowed_grades", "A+,A"))
    shadow_grades = _csv_upper_set(profile.get("ranging_probe_shadow_grades", "B"))
    shadow_only = grade in shadow_grades and grade not in allowed_grades
    theme = str(setup_context.get("theme") or _ticker_theme(ticker)).lower()
    blocked_themes = {item.lower() for item in _csv_upper_set(profile.get("ranging_probe_blocked_themes", ""))}

    def reject(reason: str, probe_eligible: bool = True, **detail) -> dict:
        payload = {
            "allowed": False,
            "probe_eligible": probe_eligible,
            "reason_not_probed": reason,
            "block_reason": block_reason,
            "grade": grade,
            "theme": theme,
            **detail,
        }
        setup_context["probe_eligible"] = probe_eligible
        setup_context["reason_not_probed"] = reason
        setup_context["ranging_probe_detail"] = payload
        return payload

    if not bool(profile.get("ranging_probe_enabled", False)):
        return reject("ranging_probe_disabled", probe_eligible=False)
    if grade not in allowed_grades and not shadow_only:
        return reject(
            "grade_not_probe_allowed",
            probe_eligible=False,
            allowed_grades=sorted(allowed_grades),
            shadow_grades=sorted(shadow_grades),
        )
    if block_reason not in {
        "ranging_regime_grade_veto",
        "ranging_regime_a_plus_quality_veto",
        "ranging_regime_a_grade_quality_veto",
    }:
        return reject("block_reason_not_probeable")
    if theme in blocked_themes:
        return reject("theme_probe_blocked")

    try:
        net_ev = float(ev_result.get("net_ev_pct"))
    except (TypeError, ValueError):
        net_ev = None
    min_ev = float(profile.get("ranging_probe_min_ev_pct", 0.03))
    if net_ev is None or net_ev < min_ev:
        return reject("ev_below_probe_min", net_ev_pct=net_ev, min_ev_pct=min_ev)

    composite = abs(float(setup_context.get("composite") or 0))
    min_composite = float(profile.get("ranging_probe_min_composite", 0.20))
    if shadow_only and grade == "B":
        min_composite = max(
            min_composite,
            float(profile.get("ranging_probe_grade_b_min_composite", 0.40)),
        )
    if composite < min_composite:
        return reject("composite_below_probe_min", composite=round(composite, 4), min_composite=min_composite)

    breakout_quality = float(setup_context.get("breakout_quality") or 0)
    min_breakout = float(profile.get("ranging_probe_min_breakout_quality", 0.35))
    if shadow_only and grade == "B":
        min_breakout = max(
            min_breakout,
            float(profile.get("ranging_probe_grade_b_min_breakout_quality", 0.60)),
        )
    if breakout_quality < min_breakout:
        return reject(
            "breakout_quality_below_probe_min",
            breakout_quality=round(breakout_quality, 4),
            min_breakout_quality=min_breakout,
        )

    sector_momentum = setup_context.get("sector_momentum") or {}
    relative_pct = sector_momentum.get("relative_pct")
    if relative_pct is not None:
        min_relative = float(profile.get("ranging_probe_min_sector_relative_pct", -0.50))
        if float(relative_pct) < min_relative:
            return reject(
                "sector_relative_strength_too_weak",
                sector_relative_pct=round(float(relative_pct), 4),
                min_sector_relative_pct=min_relative,
            )

    directional = {
        "macd_crossover": _signal_score(signals_snap, "macd_crossover") * direction,
        "tape_aggression": _signal_score(signals_snap, "tape_aggression") * direction,
        "relative_strength": _signal_score(signals_snap, "relative_strength") * direction,
    }
    max_tape_against = abs(float(profile.get("ranging_probe_max_tape_against", 0.05)))
    if directional["tape_aggression"] < -max_tape_against:
        return reject(
            "tape_opposes_probe",
            tape_aggression=round(directional["tape_aggression"], 4),
            max_tape_against=-max_tape_against,
        )

    thresholds = {
        "macd_crossover": float(profile.get("ranging_probe_min_macd", 0.10)),
        "tape_aggression": float(profile.get("ranging_probe_min_tape", 0.10)),
        "relative_strength": float(profile.get("ranging_probe_min_relative_strength", 0.25)),
    }
    aligned = [name for name, score in directional.items() if score >= thresholds[name]]
    min_aligned = int(profile.get("ranging_probe_min_aligned_signals", 2))
    if len(aligned) < min_aligned:
        return reject(
            "too_few_probe_momentum_signals",
            aligned_signals=aligned,
            aligned_count=len(aligned),
            min_aligned_signals=min_aligned,
            directional_scores={k: round(v, 4) for k, v in directional.items()},
        )

    size_multiplier = float(profile.get("ranging_probe_size_multiplier", 0.35))
    if shadow_only:
        shadow_size_multiplier = float(profile.get("ranging_probe_grade_b_shadow_size_multiplier", 0.20))
        setup_context["probe_eligible"] = True
        setup_context["reason_not_probed"] = "b_grade_shadow_only"
        setup_context["ranging_probe_shadow"] = True
        setup_context["ranging_probe_detail"] = {
            "allowed": False,
            "probe_eligible": True,
            "reason_not_probed": "b_grade_shadow_only",
            "grade": grade,
            "block_reason": block_reason,
            "aligned_signals": aligned,
            "directional_scores": {k: round(v, 4) for k, v in directional.items()},
            "net_ev_pct": net_ev,
            "hypothetical_size_multiplier": shadow_size_multiplier,
            "theme": theme,
            "sector_relative_pct": relative_pct,
            "composite": round(composite, 4),
            "breakout_quality": round(breakout_quality, 4),
            "promotion_gate": {
                "min_samples": int(profile.get("ranging_probe_grade_b_promote_min_samples", 8)),
                "min_win_rate": float(profile.get("ranging_probe_grade_b_promote_min_win_rate", 0.55)),
                "requires_avg_mfe_gt_abs_avg_mae": bool(
                    profile.get("ranging_probe_grade_b_promote_requires_mfe_gt_mae", True)
                ),
                "promote_size_multiplier": float(
                    profile.get("ranging_probe_grade_b_promote_size_multiplier", 0.20)
                ),
            },
        }
        return setup_context["ranging_probe_detail"]

    current_multiplier = float(ev_result.get("size_multiplier") or 1.0)
    ev_result["size_multiplier"] = min(current_multiplier, size_multiplier)
    ev_result["ev_decision"] = "ranging_regime_probe"
    ev_result["decision"] = "proceed"
    ev_result["ranging_probe"] = True
    setup_context["ranging_probe"] = True
    setup_context["probe_eligible"] = True
    setup_context["reason_not_probed"] = None
    setup_context["ranging_probe_detail"] = {
        "grade": grade,
        "block_reason_overridden": block_reason,
        "aligned_signals": aligned,
        "directional_scores": {k: round(v, 4) for k, v in directional.items()},
        "net_ev_pct": net_ev,
        "size_multiplier": ev_result["size_multiplier"],
        "theme": theme,
        "sector_relative_pct": relative_pct,
    }
    return {"allowed": True, **setup_context["ranging_probe_detail"]}


def _ranging_regime_block(ticker: str, setup_context: dict, ev_result: dict,
                          setup_grade: Optional[SetupGrade], profile: dict,
                          signals_snap: dict = None) -> Optional[dict]:
    if str((setup_context or {}).get("intraday_regime", "")).lower() != "ranging":
        return None
    grade = setup_grade.grade if setup_grade else (setup_context or {}).get("setup_grade")
    breakout_quality = float((setup_context or {}).get("breakout_quality") or 0)
    net_ev = (ev_result or {}).get("net_ev_pct")
    net_ev = float(net_ev) if net_ev is not None else None
    min_grade = str(profile.get("ranging_min_grade_required", "A+")).upper()
    if grade_sort_key(grade or "C") < grade_sort_key(min_grade):
        probe = _ranging_probe_decision(
            ticker, setup_context, ev_result, grade, profile,
            "ranging_regime_grade_veto", signals_snap,
        )
        if probe.get("allowed"):
            return None
        block = {
            "reason": "ranging_regime_grade_veto",
            "grade": grade,
            "min_grade": min_grade,
            "breakout_quality": round(breakout_quality, 4),
        }
        block["probe"] = probe
        return block

    if _is_leveraged_etf(ticker, profile):
        min_lev_ev = float(profile.get("ranging_leveraged_min_ev_pct", 0.25))
        if net_ev is None or net_ev < min_lev_ev:
            return {
                "reason": "ranging_regime_leveraged_ev_veto",
                "net_ev_pct": net_ev,
                "min_ev_pct": min_lev_ev,
            }

    if grade == "A+":
        composite = abs(float((setup_context or {}).get("composite") or 0))
        min_composite = float(profile.get("ranging_a_plus_min_composite", 0.25))
        min_breakout = float(profile.get("ranging_a_plus_min_breakout_quality", 0.70))
        min_ev = float(profile.get("ranging_a_plus_min_ev_pct", 0.20))
        if composite < min_composite or breakout_quality < min_breakout or net_ev is None or net_ev < min_ev:
            probe = _ranging_probe_decision(
                ticker, setup_context, ev_result, grade, profile,
                "ranging_regime_a_plus_quality_veto", signals_snap,
            )
            if probe.get("allowed"):
                return None
            block = {
                "reason": "ranging_regime_a_plus_quality_veto",
                "grade": grade,
                "composite": round(composite, 4),
                "min_composite": min_composite,
                "breakout_quality": round(breakout_quality, 4),
                "min_breakout_quality": min_breakout,
                "net_ev_pct": net_ev,
                "min_ev_pct": min_ev,
            }
            block["probe"] = probe
            return block

    if grade == "A":
        min_breakout = float(profile.get("ranging_a_grade_min_breakout_quality", 0.80))
        min_ev = float(profile.get("ranging_a_grade_min_ev_pct", 0.25))
        if breakout_quality < min_breakout or net_ev is None or net_ev < min_ev:
            probe = _ranging_probe_decision(
                ticker, setup_context, ev_result, grade, profile,
                "ranging_regime_a_grade_quality_veto", signals_snap,
            )
            if probe.get("allowed"):
                return None
            block = {
                "reason": "ranging_regime_a_grade_quality_veto",
                "grade": grade,
                "breakout_quality": round(breakout_quality, 4),
                "min_breakout_quality": min_breakout,
                "net_ev_pct": net_ev,
                "min_ev_pct": min_ev,
            }
            block["probe"] = probe
            return block

    return None


def _llm_rationale_mentions_conflict(llm_result: dict) -> bool:
    rationale = str((llm_result or {}).get("rationale") or "").lower()
    conflict_terms = ("conflict", "mixed", "disagree", "diverg", "near-zero", "near zero")
    return any(term in rationale for term in conflict_terms)


def _known_negative_grade_override_block(ev_result: dict, profile: dict) -> Optional[dict]:
    ev_result = ev_result or {}
    ev_net_pct = ev_result.get("net_ev_pct")
    if ev_net_pct is None:
        return None
    ev_sample_size = int(ev_result.get("sample_size") or 0)
    min_known_samples = int(profile.get("grade_ev_override_negative_min_samples", 10))
    ev_net_pct = float(ev_net_pct)
    if ev_net_pct < 0 and ev_sample_size >= min_known_samples:
        return {
            "net_ev_pct": ev_net_pct,
            "sample_size": ev_sample_size,
            "min_samples": min_known_samples,
        }
    return None


def _probe_floor_inflation_block(ev_decision: str, grade_min_notional_applied: bool,
                                 intended_size_eur: float, final_size_eur: float,
                                 profile: dict) -> Optional[dict]:
    if not (_is_probe_ev_decision(ev_decision) and grade_min_notional_applied and intended_size_eur > 0):
        return None
    inflation_multiple = float(final_size_eur or 0) / float(intended_size_eur)
    max_inflation = float(profile.get("probe_floor_inflation_max_multiple", 1.25))
    if inflation_multiple > max_inflation:
        return {
            "inflation_multiple": round(inflation_multiple, 3),
            "max_inflation": max_inflation,
        }
    return None


def _event_risk_active(ticker: str) -> dict:
    try:
        from backend.earnings.scanner import get_cached_earnings_guard
        info = (get_cached_earnings_guard() or {}).get(str(ticker or "").upper(), {}) or {}
        return info if info.get("blocked") else {}
    except Exception:
        return {}


def _overnight_event_risk_active(ticker: str) -> dict:
    """Return cached overnight event/filing risk if the guard has data."""
    info = _event_risk_active(ticker)
    if not info:
        return {}
    days = info.get("days_to_filing")
    try:
        if days is not None and int(days) <= 1:
            return info
    except (TypeError, ValueError):
        pass
    return info if info.get("blocked") else {}


def _breakout_quality(side: str, composite: float, signals: dict, market_regime: str = None) -> float:
    side = str(side or "").upper()
    direction = 1 if side == "BUY" else -1
    macd = _signal_score(signals, "macd_crossover") * direction
    tape = _signal_score(signals, "tape_aggression") * direction
    rel_strength = _signal_score(signals, "relative_strength") * direction
    news = _signal_score(signals, "news_sentiment") * direction
    vwap = _signal_score(signals, "vwap_deviation") * direction
    composite_aligned = float(composite or 0) * direction
    market = str(market_regime or "").lower()
    market_bonus = 1.0 if side == "BUY" and market in {"bull", "transitioning", ""} else 0.5
    if side == "SELL" and market == "bear":
        market_bonus = 1.0

    components = [
        max(0.0, min(macd, 1.0)),
        max(0.0, min(tape, 1.0)),
        max(0.0, min(rel_strength, 1.0)),
        max(0.0, min(news, 1.0)),
        max(0.0, min(abs(vwap) / 0.8, 1.0)) if vwap < 0 else 0.0,
        max(0.0, min(composite_aligned / 0.5, 1.0)),
        market_bonus,
    ]
    return round(sum(components) / len(components), 4)


def _candidate_rank_score(composite: float, breakout_quality: float, strategy_family: str,
                          event_risk_active: bool = False) -> float:
    strategy_bonus = {
        "trend_following": 0.12,
        "signal_composite": 0.04,
        "mean_reversion": -0.08,
        "direct_short": -0.03,
    }.get(str(strategy_family or ""), 0.0)
    event_penalty = 0.08 if event_risk_active else 0.0
    score = (abs(float(composite or 0)) * 0.45) + (float(breakout_quality or 0) * 0.55)
    return round(max(0.0, score + strategy_bonus - event_penalty), 4)


def _trade_setup_context(ticker: str, action: str, composite: float,
                         signals: dict, signal_result: dict,
                         regime_state, gate_reason: str = None) -> dict:
    strategy_family = _strategy_family(
        ticker, action, getattr(regime_state, "intraday_regime", None), signal_result,
        mean_reversion_trade=bool(signal_result.get("mean_reversion_signal")),
    )
    event_info = _event_risk_active(ticker)
    event_probe = bool(
        event_info
        and gate_reason
        and str(gate_reason).startswith("event_risk_intraday_probe")
    )
    breakout_quality = _breakout_quality(
        action, composite, signals, getattr(regime_state, "market_regime", None)
    )
    atr_data = signal_result.get("atr_data") or {}
    minutes_since_open = _minutes_since_regular_open()
    is_leveraged = _is_leveraged_etf(str(ticker or "").upper(), PROFILE)
    return {
        "ticker": ticker,
        "action": action,
        "strategy_family": strategy_family,
        "intraday_regime": getattr(regime_state, "intraday_regime", None),
        "market_regime": getattr(regime_state, "market_regime", None),
        "breakout_quality": breakout_quality,
        "candidate_rank_score": _candidate_rank_score(
            composite, breakout_quality, strategy_family, bool(event_info)
        ),
        "event_risk_active": bool(event_info),
        "event_risk_intraday_probe": event_probe,
        "event_risk_info": event_info,
        "minutes_since_open": minutes_since_open,
        "atr_pct": atr_data.get("atr_pct"),
        "volatility_regime": atr_data.get("volatility_regime"),
        "is_leveraged_etf": is_leveraged,
    }


def _record_blocked_opportunity(ticker: str, action: str, composite: float,
                                signals: dict, setup_context: dict,
                                regime: str, block_stage: str, block_reason: str,
                                ev_result: dict = None, reference_price: float = None,
                                block_detail: dict = None):
    try:
        payload = {
            "ticker": str(ticker or "").upper(),
            "action_hint": action,
            "composite_score": round(float(composite or 0), 4),
            "block_stage": block_stage,
            "block_reason": block_reason,
            "block_detail": block_detail or {},
            "candidate_rank_score": (setup_context or {}).get("candidate_rank_score"),
            "breakout_quality": (setup_context or {}).get("breakout_quality"),
            "ev_decision": (ev_result or {}).get("ev_decision"),
            "ev_net_pct": (ev_result or {}).get("net_ev_pct"),
            "ev_result_json": ev_result or {},
            "signals_json": {k: {"score": v.get("score", 0)} for k, v in (signals or {}).items() if isinstance(v, dict)},
            "setup_context_json": setup_context or {},
            "regime": regime,
            "market_regime": (setup_context or {}).get("market_regime"),
            "strategy_family": (setup_context or {}).get("strategy_family"),
            "event_risk_active": bool((setup_context or {}).get("event_risk_active")),
            "reference_price": reference_price,
            "setup_grade": (setup_context or {}).get("setup_grade"),
            "a_plus_blocked": (setup_context or {}).get("setup_grade") == "A+",
            "minutes_since_open": (setup_context or {}).get("minutes_since_open"),
            "atr_pct": (setup_context or {}).get("atr_pct"),
            "volatility_bucket": (setup_context or {}).get("volatility_regime"),
            "is_leveraged_etf": (setup_context or {}).get("is_leveraged_etf"),
            "probe_eligible": bool((setup_context or {}).get("probe_eligible", False)),
            "reason_not_probed": (setup_context or {}).get("reason_not_probed") or "not_probe_eligible",
        }
        result = insert_blocked_opportunity(payload)
        if result.get("error"):
            return
    except Exception:
        return


def _threshold_block_detail(action: str, composite: float, profile: dict,
                            market_regime: str = None) -> dict:
    """Structured analytics for threshold misses; does not affect gate behavior."""
    action = str(action or "").upper()
    try:
        score = abs(float(composite or 0.0))
    except (TypeError, ValueError):
        score = 0.0
    threshold = float(profile.get("min_signal_score", 0.0) or 0.0)
    if action == "SELL":
        threshold = float(profile.get("min_short_signal_score", threshold) or threshold)
        if str(market_regime or "").lower() == "bull":
            threshold = float(profile.get("bull_short_signal_score", threshold) or threshold)
    gap = threshold - score
    if threshold <= 0 or gap < 0:
        return {}
    margin = _env_float("NEAR_THRESHOLD_MARGIN", 0.01)
    return {
        "kind": "signal_threshold",
        "score": round(score, 4),
        "threshold": round(threshold, 4),
        "threshold_gap": round(gap, 4),
        "near_threshold": gap <= margin,
        "near_threshold_margin": margin,
    }


def _parse_supabase_time(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _replay_price_window(ticker: str, action: str, start_at: datetime,
                         reference_price: float, horizon_minutes: int,
                         period: str = "5d") -> dict:
    import yfinance as yf

    ticker = str(ticker or "").upper()
    action = str(action or "BUY").upper()
    if not ticker or not start_at:
        return {}
    if start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=timezone.utc)

    try:
        reference_price = float(reference_price)
    except (TypeError, ValueError):
        reference_price = 0

    now_utc = datetime.now(timezone.utc)
    end_at = min(now_utc, start_at + timedelta(minutes=horizon_minutes))
    if end_at <= start_at:
        return {}

    bars = yf.download(
        ticker,
        period=period,
        interval="1m",
        progress=False,
        auto_adjust=True,
    )
    if bars.empty:
        return {}

    if bars.index.tz is None:
        bars.index = bars.index.tz_localize("UTC")
    else:
        bars.index = bars.index.tz_convert("UTC")

    window = bars[(bars.index >= start_at) & (bars.index <= end_at)]
    if window.empty:
        return {}

    close_series = window["Close"].squeeze()
    high_series = window["High"].squeeze()
    low_series = window["Low"].squeeze()
    if reference_price <= 0:
        reference_price = float(close_series.iloc[0])
    if reference_price <= 0:
        return {}

    if action == "SELL":
        max_favorable = (reference_price - float(low_series.min())) / reference_price * 100
        max_adverse = (reference_price - float(high_series.max())) / reference_price * 100
        close_after = (reference_price - float(close_series.iloc[-1])) / reference_price * 100
    else:
        max_favorable = (float(high_series.max()) - reference_price) / reference_price * 100
        max_adverse = (float(low_series.min()) - reference_price) / reference_price * 100
        close_after = (float(close_series.iloc[-1]) - reference_price) / reference_price * 100

    return {
        "max_favorable_pct": round(max_favorable, 4),
        "max_adverse_pct": round(max_adverse, 4),
        "close_after_pct": round(close_after, 4),
        "bars_seen": int(len(window)),
        "reference_price": round(reference_price, 4),
        "start": start_at.isoformat(),
        "end": end_at.isoformat(),
        "action": action,
    }


def _replay_one_blocked_opportunity(opp: dict, horizon_minutes: int) -> dict:
    ticker = str(opp.get("ticker") or "").upper()
    action = str(opp.get("action_hint") or "BUY").upper()
    created_at = _parse_supabase_time(opp.get("created_at"))
    if not ticker or not created_at:
        return {}

    reference_price = opp.get("reference_price")
    try:
        reference_price = float(reference_price)
    except (TypeError, ValueError):
        reference_price = 0

    replay = _replay_price_window(
        ticker, action, created_at, reference_price, horizon_minutes, period="5d"
    )
    if not replay:
        return {}

    favorable = float(replay.get("max_favorable_pct") or 0.0)
    close_after = float(replay.get("close_after_pct") or 0.0)
    runner_threshold = _env_float("MISSED_RUNNER_FAVORABLE_THRESHOLD_PCT", 2.0)
    minor_threshold = _env_float("MISSED_WINNER_FAVORABLE_THRESHOLD_PCT", 0.75)
    runner_severity = None
    if favorable >= runner_threshold and close_after > 0:
        runner_severity = "runner"
    elif favorable >= minor_threshold and close_after > 0:
        runner_severity = "minor"

    return {
        "max_favorable_pct": replay["max_favorable_pct"],
        "max_adverse_pct": replay["max_adverse_pct"],
        "close_after_pct": replay["close_after_pct"],
        "replay_result_json": {
            "horizon_minutes": horizon_minutes,
            "bars_seen": replay["bars_seen"],
            "reference_price": replay["reference_price"],
            "start": replay["start"],
            "end": replay["end"],
            "action": action,
            "block_stage": opp.get("block_stage"),
            "block_reason": opp.get("block_reason"),
            "block_detail": opp.get("block_detail") or {},
            "missed_runner": runner_severity == "runner",
            "runner_severity": runner_severity,
            "runner_threshold_pct": runner_threshold,
        },
    }


def _replay_blocked_opportunities():
    if not _env_bool("BLOCKED_OPPORTUNITY_REPLAY_ENABLED", True):
        return

    min_age = _env_int("BLOCKED_OPPORTUNITY_REPLAY_MIN_AGE_MINUTES", 20)
    horizon = _env_int("BLOCKED_OPPORTUNITY_REPLAY_HORIZON_MINUTES", 90)
    limit = _env_int("BLOCKED_OPPORTUNITY_REPLAY_LIMIT", 25)
    newest_first = _env_bool("BLOCKED_OPPORTUNITY_REPLAY_NEWEST_FIRST", True)
    opportunities = get_unchecked_blocked_opportunities(
        min_age_minutes=min_age,
        limit=limit,
        newest_first=newest_first,
    )
    if not opportunities:
        return

    checked = 0
    updated = 0
    skipped = 0
    for opp in opportunities:
        checked += 1
        try:
            replay = _replay_one_blocked_opportunity(opp, horizon)
            if not replay:
                # No bars returned — mark as checked so this row is never retried.
                update_blocked_opportunity_replay(opp.get("id"), {
                    "max_favorable_pct": None,
                    "max_adverse_pct": None,
                    "close_after_pct": None,
                    "replay_result_json": {"skipped": "no_bars", "horizon_minutes": horizon},
                })
                skipped += 1
                continue
            result = update_blocked_opportunity_replay(opp.get("id"), replay)
            if not result.get("error"):
                updated += 1
        except Exception as e:
            log_event("WARN", "blocked_opportunity_replay_failed", {
                "ticker": opp.get("ticker"),
                "id": opp.get("id"),
                "error": str(e)[:160],
            })
    if checked:
        created_dates = {}
        for opp in opportunities:
            created = str(opp.get("created_at") or "")[:10] or "unknown"
            created_dates[created] = created_dates.get(created, 0) + 1
        log_event("INFO", "blocked_opportunity_replay_complete", {
            "checked": checked,
            "updated": updated,
            "skipped_no_bars": skipped,
            "horizon_minutes": horizon,
            "newest_first": newest_first,
            "created_dates": created_dates,
            "oldest_created_at": min((str(o.get("created_at")) for o in opportunities if o.get("created_at")), default=None),
            "newest_created_at": max((str(o.get("created_at")) for o in opportunities if o.get("created_at")), default=None),
        })


def _closed_trade_replay_exit_reasons() -> list[str]:
    raw = _env_value(
        "CLOSED_TRADE_REPLAY_EXIT_REASONS",
        "time_exit,eod_cleanup,thesis_invalidated,momentum_peak_decay,"
        "take_profit,stop_loss,chandelier_stop,leveraged_etf_time_exit,"
        "partial_runner_stop,a_plus_override",
    )
    return [part.strip() for part in raw.split(",") if part.strip()]


def _replay_one_closed_trade_exit(trade: dict, horizon_minutes: int) -> dict:
    ticker = str(trade.get("ticker") or "").upper()
    side = str(trade.get("side") or "BUY").upper()
    exited_at = _parse_supabase_time(
        trade.get("exit_time") or trade.get("created_at")
    )
    if not ticker or not exited_at:
        return {}

    try:
        exit_price = float(trade.get("exit_price") or 0)
    except (TypeError, ValueError):
        exit_price = 0

    replay = _replay_price_window(
        ticker, side, exited_at, exit_price, horizon_minutes, period="5d"
    )
    if not replay:
        return {}

    result_json = {
        "horizon_minutes": horizon_minutes,
        "bars_seen": replay["bars_seen"],
        "reference_price": replay["reference_price"],
        "start": replay["start"],
        "end": replay["end"],
        "action": side,
        "exit_reason": trade.get("exit_reason"),
        "net_pnl_pct": trade.get("net_pnl_pct"),
        "setup_grade": trade.get("setup_grade"),
    }
    return {
        "post_exit_horizon_minutes": horizon_minutes,
        "post_exit_max_favorable_pct": replay["max_favorable_pct"],
        "post_exit_max_adverse_pct": replay["max_adverse_pct"],
        "post_exit_close_after_pct": replay["close_after_pct"],
        "post_exit_result_json": result_json,
    }


def _replay_closed_trade_exits():
    if not _env_bool("CLOSED_TRADE_REPLAY_ENABLED", True):
        return

    min_age = _env_int("CLOSED_TRADE_REPLAY_MIN_AGE_MINUTES", 20)
    horizon = _env_int("CLOSED_TRADE_REPLAY_HORIZON_MINUTES", 120)
    limit = _env_int("CLOSED_TRADE_REPLAY_LIMIT", 25)
    max_age_days = _env_int("CLOSED_TRADE_REPLAY_MAX_AGE_DAYS", 5)
    trades = get_unchecked_closed_trades_for_replay(
        min_age_minutes=min_age,
        limit=limit,
        exit_reasons=_closed_trade_replay_exit_reasons(),
        max_age_days=max_age_days,
    )
    if not trades:
        return

    checked = 0
    updated = 0
    skipped = 0
    for trade in trades:
        checked += 1
        try:
            replay = _replay_one_closed_trade_exit(trade, horizon)
            if not replay:
                # No bars returned — mark as checked so this row is never retried.
                result = update_trade_post_exit_replay(trade.get("id"), {
                    "post_exit_horizon_minutes": horizon,
                    "post_exit_result_json": {
                        "status": "skipped_no_bars",
                        "horizon_minutes": horizon,
                        "exit_reason": trade.get("exit_reason"),
                        "ticker": trade.get("ticker"),
                    },
                })
                if not result.get("error"):
                    skipped += 1
                continue
            result = update_trade_post_exit_replay(trade.get("id"), replay)
            if not result.get("error"):
                updated += 1
        except Exception as e:
            log_event("WARN", "closed_trade_exit_replay_failed", {
                "ticker": trade.get("ticker"),
                "id": trade.get("id"),
                "exit_reason": trade.get("exit_reason"),
                "error": str(e)[:160],
            })
    if checked:
        log_event("INFO", "closed_trade_exit_replay_complete", {
            "checked": checked,
            "updated": updated,
            "skipped_no_bars": skipped,
            "horizon_minutes": horizon,
        })


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


def _ticker_loss_cooldown_active(ticker: str, side: str, recent_trades: list,
                                 profile: dict) -> Optional[dict]:
    """Pause a ticker after repeated same-day same-direction losses."""
    max_losses = _env_int("TICKER_DAILY_LOSS_COOLDOWN_COUNT", int(profile.get("ticker_daily_loss_cooldown_count", 2)))
    if max_losses <= 0:
        return None
    min_reentry_score = float(os.getenv(
        "TICKER_LOSS_REENTRY_MIN_SCORE",
        str(profile.get("ticker_loss_reentry_min_score", 0.55)),
    ))
    today = datetime.utcnow().date()
    losses = []
    for trade in recent_trades or []:
        if str(trade.get("ticker", "")).upper() != ticker.upper():
            continue
        if str(trade.get("side", "")).upper() != str(side or "").upper():
            continue
        closed_at = _parse_dt(trade.get("exit_time") or trade.get("created_at") or trade.get("closed_at"))
        if closed_at.date() != today:
            continue
        if float(trade.get("net_pnl_pct") or 0) < 0:
            losses.append(trade)
    if len(losses) >= max_losses:
        return {
            "ticker": ticker,
            "side": side,
            "losses_today": len(losses),
            "min_reentry_score": min_reentry_score,
        }
    return None


def _thesis_invalidated_cooldown_active(ticker: str, side: str, recent_trades: list,
                                        profile: dict) -> Optional[dict]:
    """DB-backed cooldown after fast thesis invalidation churn."""
    cooldown_minutes = _env_int(
        "THESIS_INVALIDATED_COOLDOWN_MINUTES",
        int(profile.get("thesis_invalidated_cooldown_minutes", 75)),
    )
    if cooldown_minutes <= 0:
        return None
    now = datetime.utcnow()
    latest = None
    for trade in recent_trades or []:
        if str(trade.get("ticker", "")).upper() != ticker.upper():
            continue
        if str(trade.get("side", "")).upper() != str(side or "").upper():
            continue
        if trade.get("exit_reason") != "thesis_invalidated":
            continue
        closed_at = _parse_dt(trade.get("exit_time") or trade.get("closed_at") or trade.get("created_at"))
        if latest is None or closed_at > latest:
            latest = closed_at
    if latest is None:
        return None
    elapsed = (now - latest).total_seconds() / 60
    if elapsed < cooldown_minutes:
        return {
            "ticker": ticker,
            "side": side,
            "minutes_since_thesis_invalidated": round(elapsed, 1),
            "cooldown_minutes": cooldown_minutes,
        }
    return None


def _ranging_stop_loss_cooldown_active(ticker: str, side: str, recent_trades: list,
                                       profile: dict) -> Optional[dict]:
    """Pause a same-ticker re-entry after a stop loss in choppy intraday regime."""
    cooldown_minutes = _env_int(
        "RANGING_STOP_LOSS_COOLDOWN_MINUTES",
        int(profile.get("ranging_stop_loss_cooldown_minutes", 90)),
    )
    if cooldown_minutes <= 0:
        return None
    now = datetime.utcnow()
    latest = None
    for trade in recent_trades or []:
        if str(trade.get("ticker", "")).upper() != ticker.upper():
            continue
        if str(trade.get("side", "")).upper() != str(side or "").upper():
            continue
        if trade.get("exit_reason") != "stop_loss":
            continue
        closed_at = _parse_dt(trade.get("exit_time") or trade.get("closed_at") or trade.get("created_at"))
        if latest is None or closed_at > latest:
            latest = closed_at
    if latest is None:
        return None
    elapsed = (now - latest).total_seconds() / 60
    if elapsed < cooldown_minutes:
        return {
            "ticker": ticker,
            "side": side,
            "minutes_since_stop_loss": round(elapsed, 1),
            "cooldown_minutes": cooldown_minutes,
        }
    return None


def _rehydrated_open_trade(record: dict) -> dict:
    sizing = record.get("sizing_json") or {}
    entry_price = float(record.get("entry_price") or 0)
    return {
        "entry_time": _parse_dt(record.get("entry_time") or record.get("created_at")),
        "entry_price": entry_price,
        "quantity": float(record.get("quantity") or record.get("submitted_qty") or 0),
        "stop_price": float(record.get("stop_price") or 0),
        "take_profit_price": float(record.get("take_profit_price") or 0),
        "hold_minutes": int(record.get("hold_minutes") or 30),
        "hold_days": int(record.get("hold_days") or 0),
        "horizon": record.get("horizon") or "short",
        "size_eur": float(record.get("size_eur") or 0),
        "size_usd": float(record.get("size_usd") or 0),
        "intended_size_eur": float(record.get("intended_size_eur") or record.get("size_eur") or 0),
        "executed_size_eur": float(record.get("executed_size_eur") or record.get("size_eur") or 0),
        "executed_size_usd": float(record.get("executed_size_usd") or record.get("size_usd") or 0),
        "submitted_qty": float(record.get("submitted_qty") or record.get("quantity") or 0),
        "implied_qty": float(record.get("implied_qty") or record.get("quantity") or 0),
        "bracket_floor_qty_loss_pct": float(record.get("bracket_floor_qty_loss_pct") or 0),
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
        "sizing_json": sizing,
        "mean_reversion_trade": bool(record.get("mean_reversion_trade") or False),
        "swing_trade": bool(record.get("swing_trade") or False),
        "promoted_to_swing": bool(record.get("promoted_to_swing") or False),
        "promoted_at": record.get("promoted_at"),
        "initial_horizon": record.get("initial_horizon") or record.get("horizon") or "short",
        "swing_conviction": float(record.get("swing_conviction") or 0),
        "swing_reasons": record.get("swing_reasons") or [],
        "highest_price_since_entry": float(
            record.get("highest_price_since_entry") or entry_price or 0
        ),
        "trailing_stop_price": float(record.get("trailing_stop_price") or 0),
        "stop_multiplier": float(record.get("stop_multiplier") or sizing.get("stop_multiplier") or 1.5),
        "stop_pct": float(record.get("stop_pct") or sizing.get("stop_pct") or 0),
        "atr_pct": float(record.get("atr_pct") or sizing.get("atr_pct") or 0),
        "atr_raw": float(record.get("atr_raw") or sizing.get("atr_raw") or 0),
        "max_hold_minutes": int(record.get("max_hold_minutes") or record.get("hold_minutes") or 30),
        "daily_reeval_count": int(record.get("daily_reeval_count") or 0),
        "hold_extension_count": int(record.get("hold_extension_count") or 0),
        "hold_decision_json": record.get("hold_decision_json") or {},
        "peak_directional_score": float(record.get("peak_directional_score") or 0),
        "protective_stop_order_id": record.get("protective_stop_order_id"),
        "llm_conviction": float(record.get("llm_conviction") or 0),
        "llm_rationale": record.get("llm_rationale") or "",
        "order_id": record.get("order_id"),
        # Grading / partial-exit fields (added in migration v3)
        "setup_grade":              record.get("setup_grade"),
        "sector_confirmation":      record.get("sector_confirmation"),
        "partial_target_price":     float(record["partial_target_price"]) if record.get("partial_target_price") is not None else None,
        "partial_exit_pct":         float(record.get("partial_exit_pct") or 0.5),
        "partial_exit_done":        bool(record.get("partial_exit_done") or False),
        "partial_exit_qty":         float(record.get("partial_exit_qty") or 0),
        "runner_atr_mult":          float(record.get("runner_atr_mult") or 0.8),
        "runner_stop_price":        float(record["runner_stop_price"]) if record.get("runner_stop_price") is not None else None,
        "vwap_thesis_strike_count": int(record.get("vwap_thesis_strike_count") or 0),
    }


def _hydrate_open_trades(broker_positions: list[dict] = None):
    """
    Rebuild runtime trade memory from persistent DB rows, then reconcile it to broker state.
    GitHub Actions runners are stateless, so the DB row is runtime memory between cycles.
    """
    records = get_open_trade_records()
    position_tickers = None
    if broker_positions is not None:
        position_tickers = _open_position_tickers({"positions": broker_positions})

    rebuilt = {}
    stale = []
    broker_closed = []
    for record in records:
        ticker = record.get("ticker")
        if not ticker:
            continue
        ticker = str(ticker).upper()
        if record.get("closed_at"):
            stale.append((ticker, "closed_at_present"))
            continue
        if position_tickers is not None and ticker not in position_tickers:
            broker_closed.append((ticker, record))
            continue
        trade = _rehydrated_open_trade(record)
        missing = [
            key for key in ("entry_time", "entry_price", "side", "order_id")
            if not trade.get(key)
        ]
        if missing:
            log_event("WARN", "open_trade_rehydrate_missing_fields", {
                "ticker": ticker,
                "missing": missing,
            })
        rebuilt[ticker] = trade

    for ticker, reason in stale:
        close_open_trade_record(ticker, reason)
        log_event("WARN", "stale_open_trade_reconciled", {
            "ticker": ticker,
            "reason": reason,
        })

    for ticker, record in broker_closed:
        trade = _rehydrated_open_trade(record)
        missing = [
            key for key in ("entry_time", "entry_price", "side", "order_id")
            if not trade.get(key)
        ]
        if missing:
            close_open_trade_record(ticker, "not_in_broker_positions")
            log_event("WARN", "stale_open_trade_reconciled", {
                "ticker": ticker,
                "reason": "not_in_broker_positions",
                "missing": missing,
            })
            continue

        # The broker no longer has the position, but Alpaca may have closed it
        # through a bracket/protective stop while this stateless runner slept.
        # Route through _close_trade so fill recovery writes the trades row.
        _open_trades[ticker] = trade
        _close_trade(
            ticker,
            trade,
            exit_price=float(trade.get("entry_price") or 0),
            exit_reason="not_in_broker_positions",
        )

    memory_stale = set(_open_trades) - set(rebuilt)
    if memory_stale:
        log_event("INFO", "open_trade_memory_rebuilt", {
            "removed": sorted(memory_stale),
            "loaded": sorted(rebuilt),
        })
    _open_trades.clear()
    _open_trades.update(rebuilt)


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
    the trade is profitable or only within a controlled loss floor, promote it
    to a 3-5 day swing instead of closing.
    Returns True if promoted (caller should skip the close).
    """
    ticker = str(ticker or "").upper()
    if ticker not in SWING_TICKERS:
        log_event("INFO", "eod_carry_blocked_not_swing_ticker", {"ticker": ticker})
        return False

    if (trade.get("entry_price") or 0) <= 0:
        return False
    entry_price = float(trade.get("entry_price") or 0)
    pnl_pct = _trade_pnl_pct(trade, current_price)
    stop_pct_for_floor = float(
        trade.get("stop_pct")
        or profile.get("stop_loss_pct")
        or 2.5
    )
    max_loss_r = _env_float(
        "EOD_CARRY_MAX_LOSS_R",
        float(profile.get("eod_carry_max_loss_r", 0.5)),
    )
    max_carry_loss_pct = -1 * abs(stop_pct_for_floor) * max_loss_r
    if pnl_pct < max_carry_loss_pct:
        log_event("INFO", "eod_carry_blocked_loss_too_deep", {
            "ticker": ticker,
            "pnl_pct": round(pnl_pct, 4),
            "max_carry_loss_pct": round(max_carry_loss_pct, 4),
            "max_loss_r": max_loss_r,
        })
        return False

    # Don't promote mean-reversion trades
    if trade.get("mean_reversion_trade"):
        return False

    # Never promote leveraged ETFs to swing — daily decay makes overnight holds dangerous
    if _is_leveraged_etf(ticker, profile):
        return False

    overnight_event_risk = _overnight_event_risk_active(ticker)
    if overnight_event_risk:
        log_event("INFO", "eod_carry_blocked_event_risk", {
            "ticker": ticker,
            "event_risk": overnight_event_risk,
        })
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

    overnight_count = sum(
        1 for t, d in _open_trades.items()
        if t != ticker and d.get("swing_trade")
    )
    max_overnight = _env_int(
        "MAX_OVERNIGHT_CARRIES",
        int(profile.get("max_overnight_carries", 1)),
    )
    if overnight_count >= max_overnight:
        log_event("INFO", "eod_carry_blocked_overnight_cap", {
            "ticker": ticker,
            "open_overnight_carries": overnight_count,
            "max_overnight_carries": max_overnight,
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
        # Never tighter than 1 ATR below entry — prevents intraday-tight stops
        # surviving into a multi-day swing hold
        protective_stop_price = min(protective_stop_price, entry_price - atr_raw)
        protective_side = "sell"
    else:
        stop_price = entry_price * (1 + stop_pct / 100)
        chandelier_stop = current_price + (atr_raw * stop_multiplier)
        protective_stop_price = min(stop_price, chandelier_stop)
        protective_stop_price = max(protective_stop_price, entry_price + atr_raw)
        protective_side = "buy"

    cancel_results = _cancel_bracket_orders_for_manual_exit(ticker, trade)
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
            "eod_decision":        "carry_overnight" if pnl_pct <= 0 else "promote_swing",
            "max_carry_loss_pct":  round(max_carry_loss_pct, 4),
            "swing_check":         swing_check,
            "cancelled_bracket_legs": cancel_results,
            "protective_stop_order": protective_order,
        },
    })

    save_result = save_open_trade(ticker, _open_trades[ticker])
    if save_result.get("error"):
        # swing_trade=True exists in memory but not DB — next cold-start will
        # treat this as an intraday trade and may EOD-exit it incorrectly.
        log_event("ERROR", "swing_promotion_save_failed", {
            "ticker": ticker,
            "error": save_result["error"],
            "swing_trade": True,
            "protective_stop_order_id": protective_order.get("order_id"),
        })

    log_event("INFO", "swing_promoted", {
        "ticker":          ticker,
        "hold_days":       hold_days,
        "conviction":      swing_check["conviction"],
        "reasons":         swing_check["reasons"],
        "pnl_at_promotion": round(pnl_pct, 3),
        "protective_stop_order_id": protective_order.get("order_id"),
        "state_persisted": not bool(save_result.get("error")),
    })
    _send_discord_alert(
        f"Swing promoted: {ticker} "
        f"{hold_days}-day hold · "
        f"Conviction: {swing_check['conviction']:.0%} · "
        f"P&L at promotion: {pnl_pct:+.1f}%"
    )
    return True


# ── Percentile window update ─────────────────────────────────────────────────

def _update_signal_percentiles(cycle_composites: dict, db_percentiles: dict):
    """Best-effort per-ticker percentile window update after each cycle."""
    for ticker, composite in cycle_composites.items():
        try:
            existing = db_percentiles.get(ticker, {})
            window = list(existing.get("window_composites") or [])
            window = merge_percentile_window(composite, window, max_window=200)
            thresholds = compute_percentile_thresholds(window)
            thresholds["window_composites"] = window
            upsert_signal_percentiles(ticker, thresholds)
        except Exception:
            pass  # never crash the cycle on percentile writes


# ── Core cycle ────────────────────────────────────────────────────────────────

def run_signal_cycle():
    """Main cycle: compute signals → gate → decide → execute."""
    global _learning_engine, _logged_sector_config_warnings

    cycle_start_utc = datetime.now(timezone.utc)

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
    _hydrate_open_trades(portfolio_state.get("positions", []))

    # Staleness guard: if this cycle started too late (queued behind a cancelled
    # run), skip signal computation and only run exit checks. Bracket orders
    # protect positions at the broker; signal decisions on stale data cause harm.
    cycle_age_seconds = (datetime.now(timezone.utc) - cycle_start_utc).total_seconds()
    stale_threshold = _env_int("CYCLE_STALENESS_THRESHOLD_SECONDS", 180)
    cycle_stale = cycle_age_seconds > stale_threshold
    if cycle_stale:
        log_event("WARN", "cycle_stale_exits_only", {
            "cycle_age_seconds": round(cycle_age_seconds),
            "threshold_seconds": stale_threshold,
        })
        _check_exits(portfolio_state, effective_profile)
        _save_snapshot(portfolio_state, regime)
        return

    log_event("INFO", "cycle_start", {
        "regime": regime, "equity": portfolio_state["equity"],
        "vix": portfolio_state["vix"], "tickers": TICKERS,
        "horizon": HORIZON, "macro_regime": macro_regime,
        "macro_meta": macro_meta,
        "regime_state": regime_state.to_dict(),
        "shock_result": shock_result,
    })
    if _SECTOR_CONFIG_WARNINGS and not _logged_sector_config_warnings:
        log_event("WARN", "sector_config_warnings", {"warnings": _SECTOR_CONFIG_WARNINGS})
        _logged_sector_config_warnings = True

    if not TICKERS:
        log_event("ERROR", "no_tickers_configured", {
            "hint": "Set TICKER_UNIVERSE or leave it unset to use config/sector_universe.toml core tickers"
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

    # Earnings proximity guard (6hr cache — populates cache read by pre_trade_gate)
    try:
        from backend.earnings.scanner import scan_earnings_guard
        eg = scan_earnings_guard(TICKERS)
        blocked = [t for t, v in eg.items() if v.get("blocked")]
        if blocked:
            log_event("INFO", "earnings_guard_active", {"blocked_tickers": blocked})
    except Exception as e:
        log_event("WARN", "earnings_guard_scan_failed", {"error": str(e)[:100]})

    if _allows_intraday():
        # Reset cycle-level state
        global _cycle_composites, _cycle_db_percentiles
        _cycle_composites = {}

        # Pre-populate DB news cache for all tickers in one batch (2-3 NewsAPI calls
        # instead of up to 22). Per-ticker news_sentiment_score calls below hit the cache.
        try:
            prefetch_newsapi_batch(TICKERS)
        except Exception:
            pass

        candidates = []
        for ticker in TICKERS:
            try:
                candidate = _evaluate_ticker_candidate(
                    ticker, regime, weights, effective_profile,
                    portfolio_state, recent_trades,
                    regime_state, shock_result,
                )
                if candidate:
                    candidates.append(candidate)
            except Exception as e:
                log_event("ERROR", f"ticker_error_{ticker}", {"error": str(e)})

        # ── Grading pass (uses full cycle composites for sector confirmation) ─
        _cycle_db_percentiles = {}
        try:
            _cycle_db_percentiles = get_signal_percentiles(list(_cycle_composites.keys()))
        except Exception as e:
            log_event("WARN", "percentile_load_failed", {"error": str(e)[:120]})

        # Update percentile windows for all tickers computed this cycle
        _update_signal_percentiles(_cycle_composites, _cycle_db_percentiles)
        sector_momentum = _sector_momentum_snapshot(TICKERS, effective_profile)
        _log_dynamic_universe_shadow(TICKERS, sector_momentum, effective_profile)
        regime_observed = Counter(
            str(c.get("ticker_regime") or "unknown") for c in candidates
        )
        log_event("INFO", "cycle_regime_observability", {
            "market_regime": getattr(regime_state, "market_regime", None),
            "intraday_regimes": dict(regime_observed),
            "spy_trend_score": getattr(regime_state, "trend_score", None),
            "spy_trend_threshold": getattr(regime_state, "trend_threshold", None),
            "spy_regime_reason": getattr(regime_state, "regime_reason", None),
        })

        min_grade = effective_profile.get("min_grade_required", "B")
        graded_candidates = []
        for candidate in candidates:
            try:
                t = candidate["ticker"]
                sector_conf = compute_sector_confirmation(t, _cycle_composites)
                pct_rank = get_ticker_percentile_rank(
                    t, candidate["composite"], _cycle_db_percentiles
                )
                setup_grade = grade_setup(
                    t,
                    candidate["composite"],
                    candidate["signals_snap"],
                    candidate["ticker_regime_state"],
                    sector_conf,
                    pct_rank,
                    candidate.get("orb_score", 0.0),
                    effective_profile,
                )
                candidate["setup_grade"] = setup_grade
                candidate["setup_context"]["setup_grade"] = setup_grade.grade
                candidate["setup_context"]["sector_confirmation"] = setup_grade.sector_confirmation
                candidate["setup_context"]["percentile_rank"] = setup_grade.percentile_rank
                _apply_sector_momentum_to_candidate(candidate, sector_momentum)
                if candidate.get("signal_id"):
                    update_signal(candidate["signal_id"], {
                        "setup_grade": setup_grade.grade,
                        "sector_confirmation": setup_grade.sector_confirmation,
                        "percentile_rank": setup_grade.percentile_rank,
                        "orb_score": candidate.get("orb_score", 0.0),
                    })

                # EV-blocked candidates: A+/A override with probe size; B/C drop
                if candidate.get("ev_blocked"):
                    if setup_grade.grade in {"A+", "A"}:
                        known_negative = _known_negative_grade_override_block(
                            candidate.get("ev_result"), effective_profile
                        )
                        if known_negative:
                            log_event("INFO", "grade_ev_override_known_negative_block", {
                                "ticker": t,
                                "grade": setup_grade.grade,
                                **known_negative,
                            })
                            _record_blocked_opportunity(
                                t, candidate.get("action_hint"), candidate["composite"],
                                candidate["signals_snap"], candidate["setup_context"],
                                candidate["ticker_regime"], "ev",
                                "known_negative_ev_grade_override_block",
                                ev_result=candidate["ev_result"],
                            )
                            continue
                        ev_override = candidate["ev_result"].copy()
                        ev_override["size_multiplier"] = 0.35
                        ev_override["ev_decision"] = "grade_ev_override_probe"
                        ev_override["decision"] = "proceed"
                        candidate["ev_result"] = ev_override
                        log_event("INFO", "ev_block_overridden_by_grade", {
                            "ticker": t, "grade": setup_grade.grade,
                            "original_reason": candidate["ev_result"].get("reason"),
                        })
                    else:
                        _record_blocked_opportunity(
                            t, candidate.get("action_hint"), candidate["composite"],
                            candidate["signals_snap"], candidate["setup_context"],
                            candidate["ticker_regime"], "ev",
                            candidate["ev_result"].get("reason", "ev_blocked"),
                            ev_result=candidate["ev_result"],
                        )
                    continue

                ranging_block = _ranging_regime_block(
                    t, candidate["setup_context"], candidate.get("ev_result"),
                    setup_grade, effective_profile,
                    signals_snap=candidate.get("signals_snap"),
                )
                if ranging_block:
                    if ranging_block.get("probe"):
                        log_event("INFO", "ranging_probe_rejected", ranging_block["probe"])
                    log_event("INFO", "ranging_regime_candidate_block", {
                        "ticker": t,
                        "composite": round(float(candidate.get("composite") or 0), 4),
                        **{k: v for k, v in ranging_block.items() if k != "probe"},
                    })
                    _record_blocked_opportunity(
                        t, candidate.get("action_hint"), candidate["composite"],
                        candidate["signals_snap"], candidate["setup_context"],
                        candidate["ticker_regime"], "regime",
                        ranging_block["reason"],
                        ev_result=candidate.get("ev_result"),
                    )
                    continue
                if candidate["setup_context"].get("ranging_probe"):
                    log_event("INFO", "ranging_probe_allowed", {
                        "ticker": t,
                        "composite": round(float(candidate.get("composite") or 0), 4),
                        **candidate["setup_context"].get("ranging_probe_detail", {}),
                    })

                # Leveraged ETFs require A+ grade — drop anything weaker
                if _is_leveraged_etf(t, effective_profile) and setup_grade.grade != "A+":
                    log_event("INFO", "leveraged_etf_grade_block", {
                        "ticker": t, "grade": setup_grade.grade,
                        "reason": "leveraged_etf_requires_a_plus",
                    })
                    _record_blocked_opportunity(
                        t, candidate.get("action_hint"), candidate["composite"],
                        candidate["signals_snap"], candidate["setup_context"],
                        candidate["ticker_regime"], "ranking",
                        "leveraged_etf_not_a_plus",
                        ev_result=candidate.get("ev_result"),
                    )
                    continue

                # Enforce minimum grade from dynamic risk budget. B setups may be kept only as tiny
                # exploration trades so learning can continue without letting them drive P&L.
                if grade_sort_key(setup_grade.grade) < grade_sort_key(min_grade):
                    if setup_grade.grade == "B" and effective_profile.get("allow_b_grade_exploration", False):
                        ev_override = (candidate.get("ev_result") or {}).copy()
                        original_multiplier = float(ev_override.get("size_multiplier") or 1.0)
                        ev_override["size_multiplier"] = min(
                            original_multiplier,
                            float(effective_profile.get("b_grade_size_multiplier", 0.20)),
                        )
                        ev_override["ev_decision"] = "b_grade_exploration_size"
                        candidate["ev_result"] = ev_override
                        log_event("INFO", "b_grade_exploration_sized", {
                            "ticker": t,
                            "grade": setup_grade.grade,
                            "min_grade": min_grade,
                            "size_multiplier": ev_override["size_multiplier"],
                        })
                        graded_candidates.append(candidate)
                        continue
                    log_event("INFO", "grade_below_minimum", {
                        "ticker": t, "grade": setup_grade.grade,
                        "min_grade": min_grade, "composite": candidate["composite"],
                    })
                    _record_blocked_opportunity(
                        t, candidate.get("action_hint"), candidate["composite"],
                        candidate["signals_snap"], candidate["setup_context"],
                        candidate["ticker_regime"], "ranking",
                        f"grade_{setup_grade.grade}_below_min_{min_grade}",
                        ev_result=candidate.get("ev_result"),
                    )
                    continue

                graded_candidates.append(candidate)
            except Exception as e:
                log_event("WARN", f"grade_error_{candidate['ticker']}", {"error": str(e)[:120]})
                _record_blocked_opportunity(
                    candidate["ticker"], candidate.get("action_hint"), candidate.get("composite", 0),
                    candidate.get("signals_snap"), candidate.get("setup_context"),
                    candidate.get("ticker_regime"), "ranking", "setup_grade_unavailable",
                    ev_result=candidate.get("ev_result"),
                )

        candidates = graded_candidates
        candidates, theme_skipped = _theme_cap_candidates(candidates, effective_profile)
        for skipped in theme_skipped:
            log_event("INFO", "candidate_theme_cap_skipped", {
                "ticker": skipped["ticker"],
                "theme": skipped.get("theme"),
                "reason": skipped.get("theme_cap_reason"),
                "rank_score": skipped.get("setup_context", {}).get("candidate_rank_score"),
            })
            _record_blocked_opportunity(
                skipped["ticker"],
                skipped.get("action_hint"),
                skipped.get("composite"),
                skipped.get("signals_snap"),
                skipped.get("setup_context"),
                skipped.get("ticker_regime"),
                "ranking",
                skipped.get("theme_cap_reason", "theme_cap"),
                ev_result=skipped.get("ev_result"),
            )
        if candidates:
            high_uniform = [
                c for c in candidates
                if (c.get("setup_grade") is not None
                    and float(c["setup_grade"].sector_confirmation or 0) >= 0.99
                    and float(c["setup_grade"].percentile_rank or 0) >= 95)
            ]
            if len(high_uniform) >= max(3, int(len(candidates) * 0.75)):
                log_event("WARN", "grading_metrics_uniform_high", {
                    "count": len(high_uniform),
                    "candidate_count": len(candidates),
                    "tickers": [c["ticker"] for c in high_uniform[:12]],
                    "reason": "sector_confirmation_and_percentile_rank_not_differentiating",
                })
        candidates.sort(
            key=lambda c: (
                grade_sort_key((c.get("setup_grade") or SetupGrade("B", 0.6, 0.5, 0.8, False, [], 0, 0.5, 40, False)).grade),
                float(c.get("setup_context", {}).get("candidate_rank_score") or 0),
                abs(float(c.get("composite") or 0)),
            ),
            reverse=True,
        )
        if candidates:
            max_per_cycle = _env_int(
                "MAX_NEW_INTRADAY_TRADES_PER_CYCLE",
                int(effective_profile.get("max_new_intraday_trades_per_cycle", 2)),
            )
            # Expand slot by 1 when 3+ full_size EV-approved setups are ready —
            # avoids dropping strong conviction tickers on high-conviction cycles.
            full_size_count = sum(
                1 for c in candidates
                if (c.get("ev_result") or {}).get("ev_decision") in ("full_size", "probe_size")
            )
            if full_size_count >= 3:
                max_per_cycle = max_per_cycle + 1
            log_event("INFO", "ranked_trade_candidates", {
                "selected": [c["ticker"] for c in candidates[:max_per_cycle]],
                "candidates": [
                    {
                        "ticker": c["ticker"],
                        "score": round(float(c.get("composite") or 0), 4),
                        "rank_score": c.get("setup_context", {}).get("candidate_rank_score"),
                        "theme": c.get("setup_context", {}).get("theme"),
                        "sector_momentum_multiplier": c.get("setup_context", {}).get("sector_momentum_multiplier"),
                        "breakout_quality": c.get("setup_context", {}).get("breakout_quality"),
                        "ev_decision": c.get("ev_result", {}).get("ev_decision"),
                        "strategy_family": c.get("setup_context", {}).get("strategy_family"),
                    }
                    for c in candidates
                ],
            })
            for skipped in candidates[max_per_cycle:]:
                log_event("INFO", "candidate_not_selected", {
                    "ticker": skipped["ticker"],
                    "reason": "lower ranked than selected candidates",
                    "rank_score": skipped.get("setup_context", {}).get("candidate_rank_score"),
                    "breakout_quality": skipped.get("setup_context", {}).get("breakout_quality"),
                    "ev_decision": skipped.get("ev_result", {}).get("ev_decision"),
                })
                _record_blocked_opportunity(
                    skipped["ticker"],
                    skipped.get("action_hint"),
                    skipped.get("composite"),
                    skipped.get("signals_snap"),
                    skipped.get("setup_context"),
                    skipped.get("ticker_regime"),
                    "ranking",
                    "lower ranked than selected candidates",
                    ev_result=skipped.get("ev_result"),
                )
            for candidate in candidates[:max_per_cycle]:
                try:
                    _execute_trade_candidate(candidate, effective_profile, portfolio_state)
                except Exception as e:
                    log_event("ERROR", f"candidate_execution_error_{candidate['ticker']}", {"error": str(e)})

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


def _evaluate_ticker_candidate(ticker, regime, weights, profile, portfolio_state, recent_trades,
                               regime_state, shock_result):
    """Signal → gate → EV. Returns a ranked candidate if execution should be considered."""
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
    # Capture for sector confirmation (used after the full evaluation loop)
    _cycle_composites[ticker] = composite
    signals_snap  = signal_result["signals"]
    atr_data       = signal_result.get("atr_data") or {}
    regime_debug   = _regime_debug_payload(ticker_regime_state, signal_result)
    news_headline = (signals_snap.get("news_sentiment", {})
                    .get("meta", {}).get("latest_headline", ""))

    # 2. Pre-trade gate (hard rules)
    capital_base = _trading_capital(portfolio_state["equity"])
    action_hint = _deterministic_action(composite)
    eod_entry_block = _is_new_intraday_entry_too_late(ticker)
    if eod_entry_block:
        setup_context = _trade_setup_context(
            ticker, action_hint, composite, signals_snap, signal_result,
            ticker_regime_state, gate_reason=eod_entry_block["reason"],
        )
        log_event("INFO", "eod_new_entry_block", {
            "ticker": ticker,
            "composite": round(composite, 4),
            **eod_entry_block,
        })
        _record_blocked_opportunity(
            ticker, action_hint, composite, signals_snap, setup_context,
            ticker_regime, "time", eod_entry_block["reason"],
        )
        return
    pre_size = compute_position_size(ticker, capital_base, profile, 0.7, atr_data, ticker_regime_state)
    size_eur = pre_size["size_eur"]
    if action_hint == "SELL":
        size_eur = _cap_short_notional(size_eur, capital_base, profile)
    cooldown = _time_exit_cooldown_active(ticker, recent_trades, profile)
    if cooldown:
        log_event("INFO", "time_exit_cooldown", cooldown)
        return
    thesis_cooldown = _thesis_invalidated_cooldown_active(ticker, action_hint, recent_trades, profile)
    if thesis_cooldown:
        log_event("INFO", "thesis_invalidated_cooldown", thesis_cooldown)
        return
    if str(ticker_regime or "").lower() == "ranging":
        stop_loss_cooldown = _ranging_stop_loss_cooldown_active(ticker, action_hint, recent_trades, profile)
        if stop_loss_cooldown:
            log_event("INFO", "ranging_stop_loss_cooldown", stop_loss_cooldown)
            return
    loss_cooldown = _ticker_loss_cooldown_active(ticker, action_hint, recent_trades, profile)
    if loss_cooldown and abs(composite) < float(loss_cooldown["min_reentry_score"]):
        loss_cooldown["composite"] = round(composite, 4)
        log_event("INFO", "ticker_loss_cooldown", loss_cooldown)
        return
    if str(ticker_regime or "").lower() == "ranging":
        ranging_cap = int(profile.get("ranging_max_trades_per_day", 6))
        if ranging_cap > 0 and int(portfolio_state.get("trades_today") or 0) >= ranging_cap:
            reason = f"ranging_regime_daily_trade_cap ({portfolio_state.get('trades_today')}/{ranging_cap})"
            log_event("INFO", "ranging_regime_trade_cap", {
                "ticker": ticker,
                "trades_today": portfolio_state.get("trades_today"),
                "ranging_max_trades_per_day": ranging_cap,
            })
            setup_context = _trade_setup_context(
                ticker, action_hint, composite, signals_snap, signal_result,
                ticker_regime_state, gate_reason=reason,
            )
            _record_blocked_opportunity(
                ticker, action_hint, composite, signals_snap, setup_context,
                ticker_regime, "regime", reason,
            )
            return
    gate_ok, gate_reason = pre_trade_gate(
        ticker, action_hint.lower(), size_eur, composite, profile, portfolio_state,
        market_regime=getattr(ticker_regime_state, "market_regime", None),
        signals=signals_snap,
    )

    # Leveraged ETF pre-entry gate (A+-only, VIX cap, no entries after 3:45 PM ET)
    if _is_leveraged_etf(ticker, profile):
        vix_now = float(portfolio_state.get("vix") or 20.0)
        lev_vix_ceiling = float(profile.get("leveraged_etf_vix_ceiling", 22))
        if vix_now >= lev_vix_ceiling:
            log_event("INFO", "leveraged_etf_vix_block", {
                "ticker": ticker, "vix": vix_now, "ceiling": lev_vix_ceiling,
            })
            return
        if _leveraged_etf_max_hold_window():
            log_event("INFO", "leveraged_etf_time_block", {
                "ticker": ticker, "reason": "past_3_45pm_et_no_new_entries",
            })
            return
        # Leveraged ETFs are A+-only entries
        # setup_grade not yet computed here — defer to the grading pass via ev_blocked flag
        # but we mark the candidate so the grading pass can enforce the A+ requirement
        gate_reason = gate_reason or {}
        if isinstance(gate_reason, dict):
            gate_reason["leveraged_etf"] = True

    setup_context = _trade_setup_context(
        ticker, action_hint, composite, signals_snap, signal_result,
        ticker_regime_state, gate_reason=gate_reason,
    )
    strategy_family_hint = setup_context["strategy_family"]
    alignment_veto = _alignment_veto(ticker, action_hint, signals_snap, profile)
    if alignment_veto:
        reason = alignment_veto["reason"]
        log_event("INFO", "signal_alignment_veto", {
            "ticker": ticker,
            "action": action_hint,
            "composite": round(composite, 4),
            **alignment_veto,
        })
        _record_blocked_opportunity(
            ticker, action_hint, composite, signals_snap, setup_context,
            ticker_regime, "signal_alignment", reason,
        )
        return
    consensus_block = _signal_consensus_block(action_hint, signals_snap, ticker_regime, profile)
    if consensus_block:
        reason = consensus_block["reason"]
        log_event("INFO", "signal_consensus_veto", {
            "ticker": ticker,
            "action": action_hint,
            "composite": round(composite, 4),
            "regime": ticker_regime,
            **consensus_block,
        })
        _record_blocked_opportunity(
            ticker, action_hint, composite, signals_snap, setup_context,
            ticker_regime, "signal_consensus", reason,
        )
        return
    theme_exposure_block = _theme_open_exposure_block(ticker, profile)
    if theme_exposure_block:
        reason = theme_exposure_block["reason"]
        log_event("INFO", "theme_open_exposure_cap", {
            "ticker": ticker,
            "action": action_hint,
            "composite": round(composite, 4),
            **theme_exposure_block,
        })
        _record_blocked_opportunity(
            ticker, action_hint, composite, signals_snap, setup_context,
            ticker_regime, "exposure", reason,
        )
        return

    # 3. Log signal to DB. Grade metadata is updated after the full cycle
    # because sector confirmation depends on all tickers' composites.
    signal_row = insert_signal({
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
        "market_regime":          getattr(ticker_regime_state, "market_regime", None),
        "regime_bull_bear":       signal_result.get("regime_bull_bear"),
        "shock_detected":         signal_result.get("shock_detected", False),
        "shock_classification":   signal_result.get("shock_classification"),
        "yield_curve":           getattr(ticker_regime_state, "yield_curve", None),
        "yield_curve_state":     getattr(ticker_regime_state, "yield_curve_state", None),
        "regime":                 ticker_regime,
        "action_hint":            action_hint,
        "exposure_direction":     _exposure_direction(ticker, action_hint),
        "strategy_family":        strategy_family_hint,
        "regime_debug_json":      regime_debug,
        "vix":                    portfolio_state["vix"],
        "gated":                  not gate_ok,
        "gate_reason":            gate_reason if not gate_ok else None,
        "llm_called":             False,
        "orb_score":              signal_result.get("orb_score", 0.0),
    })
    signal_id = signal_row.get("id") if isinstance(signal_row, dict) else None

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
        _record_blocked_opportunity(
            ticker, action_hint, composite, signals_snap, setup_context,
            ticker_regime, "gate", gate_reason,
            block_detail=(
                _threshold_block_detail(
                    action_hint, composite, profile,
                    market_regime=getattr(ticker_regime_state, "market_regime", None),
                )
                if "signal below threshold" in str(gate_reason)
                else {}
            ),
        )
        return

    # 4. EV check
    ev_result = compute_expected_value(
        composite, size_eur, recent_trades, ticker_regime,
        setup_context=setup_context,
        profile=profile,
    )
    ev_blocked = ev_result["decision"] == "block"
    if ev_blocked:
        # Don't hard-block here — carry forward to grading pass.
        # A+/A grades will override with probe size; B/C will be dropped there.
        log_event("INFO", "ev_blocked_pending_grade", {"ticker": ticker, **ev_result})
    if ev_result.get("ev_decision") not in {None, "full_size", "exploration_full_size"}:
        log_event("INFO", "ev_sizing_adjusted", {
            "ticker": ticker,
            "composite": composite,
            "ev_decision": ev_result.get("ev_decision"),
            "size_multiplier": ev_result.get("size_multiplier"),
            "breakout_quality": ev_result.get("breakout_quality"),
            "reason": ev_result.get("reason"),
            "setup": setup_context,
        })

    return {
        "ticker": ticker,
        "ticker_regime": ticker_regime,
        "ticker_regime_state": ticker_regime_state,
        "signal_result": signal_result,
        "composite": composite,
        "signals_snap": signals_snap,
        "atr_data": atr_data,
        "regime_debug": regime_debug,
        "news_headline": news_headline,
        "setup_context": setup_context,
        "ev_result": ev_result,
        "ev_blocked": ev_blocked,
        "capital_base": capital_base,
        "action_hint": action_hint,
        "orb_score": signal_result.get("orb_score", 0.0),
        "signal_id": signal_id,
    }


def _execute_trade_candidate(candidate: dict, profile: dict, portfolio_state: dict):
    """LLM → order for an already-ranked candidate."""
    ticker = candidate["ticker"]
    ticker_regime = candidate["ticker_regime"]
    ticker_regime_state = candidate["ticker_regime_state"]
    signal_result = candidate["signal_result"]
    composite = candidate["composite"]
    signals_snap = candidate["signals_snap"]
    atr_data = candidate["atr_data"]
    regime_debug = candidate["regime_debug"]
    news_headline = candidate["news_headline"]
    setup_context = candidate["setup_context"]
    ev_result = candidate["ev_result"]
    capital_base = candidate["capital_base"]

    # ── A+ setup grade — override soft LLM blocks ────────────────────────────
    setup_grade: Optional[SetupGrade] = candidate.get("setup_grade")
    is_a_plus = setup_grade is not None and setup_grade.grade == "A+"
    hard_blocks = a_plus_hard_blocks()
    action = _deterministic_action(composite)

    # 5. LLM decision (gated by hourly limit)
    llm_result = None
    if not _can_call_llm():
        if is_a_plus:
            # A+ escalation: skip LLM, proceed with probe size
            log_event("INFO", "a_plus_llm_limit_override", {
                "ticker": ticker, "composite": composite,
                "grade": setup_grade.grade, "action": action,
            })
            llm_result = {
                "action": action,
                "conviction": max(abs(composite), 0.55),
                "hold_minutes": int(profile.get("max_hold_minutes", 45)),
                "stop_loss_pct": float(profile.get("stop_loss_pct", 2.0)),
                "rationale": "a_plus_llm_limit_override",
            }
            ev_result = ev_result.copy()
            ev_result["size_multiplier"] = min(ev_result.get("size_multiplier", 1.0), 0.35)
            ev_result["ev_decision"] = "a_plus_probe"
        else:
            log_event("WARN", "llm_limit_hit", {"ticker": ticker})
            _record_blocked_opportunity(
                ticker, candidate.get("action_hint"), composite, signals_snap, setup_context,
                ticker_regime, "llm", "llm_limit_hit", ev_result=ev_result,
            )
            return

    if llm_result is None:
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
        allow_hold_override = bool(profile.get("allow_a_plus_llm_hold_override", False))
        if is_a_plus and allow_hold_override and not _llm_rationale_mentions_conflict(llm_result):
            # A+ escalation: override LLM HOLD — deterministic action with probe size
            log_event("INFO", "a_plus_llm_hold_override", {
                "ticker": ticker, "composite": composite,
                "grade": setup_grade.grade, "llm_rationale": llm_result.get("rationale", ""),
            })
            suggested_action = action
            # Force probe size — don't go full size on an LLM-overridden entry
            ev_result = ev_result.copy()
            ev_result["size_multiplier"] = min(ev_result.get("size_multiplier", 1.0), 0.35)
            ev_result["ev_decision"] = "a_plus_probe"
        else:
            log_event("INFO", "llm_hold_veto", {
                "ticker": ticker,
                "composite": composite,
                "rationale": llm_result.get("rationale", ""),
                "grade": setup_grade.grade if setup_grade else None,
                "a_plus_hold_override_enabled": allow_hold_override,
            })
            _record_blocked_opportunity(
                ticker, action, composite, signals_snap, setup_context,
                ticker_regime, "llm",
                llm_result.get("rationale", "llm_hold_veto"),
                ev_result=ev_result,
            )
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
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "llm",
            llm_result.get("rationale", "llm_direction_conflict"),
            ev_result=ev_result,
        )
        if action == "SELL":
            _log_short_candidate(
                "short_candidate_llm_conflict", ticker, composite,
                llm_result.get("rationale", "llm_direction_conflict"),
                profile, ticker_regime_state,
                {"llm_action": suggested_action, "llm_conviction": llm_conviction},
            )
        return
    if suggested_action in {"BUY", "SELL"} and _llm_rationale_mentions_conflict(llm_result):
        log_event("INFO", "llm_rationale_conflict_veto", {
            "ticker": ticker,
            "composite": composite,
            "llm_action": suggested_action,
            "rationale": llm_result.get("rationale", ""),
        })
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "llm",
            llm_result.get("rationale", "llm_rationale_conflict_veto"),
            ev_result=ev_result,
        )
        return

    if conviction < profile["min_conviction"]:
        log_event("INFO", "conviction_below_threshold", {
            "ticker": ticker,
            "conviction": conviction,
            "min_conviction": profile["min_conviction"],
        })
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "conviction", "conviction_below_threshold",
            ev_result=ev_result,
        )
        if action == "SELL":
            _log_short_candidate(
                "short_candidate_conviction_blocked", ticker, composite,
                "conviction_below_threshold", profile, ticker_regime_state,
                {"conviction": conviction, "min_conviction": profile["min_conviction"]},
            )
        return

    # 6. Size and submit order
    sizing = compute_position_size(ticker, capital_base, profile, conviction, atr_data, ticker_regime_state)
    base_stop_pct = float(sizing.get("stop_pct") or profile.get("stop_loss_pct", 2.0))
    stop_scalar = _leveraged_etf_stop_scalar(ticker, profile)
    if stop_scalar > 1.0:
        adjusted_stop_pct = min(12.0, max(base_stop_pct, base_stop_pct * stop_scalar))
        if adjusted_stop_pct > base_stop_pct:
            risk_scale = base_stop_pct / adjusted_stop_pct
            sizing["size_eur"] = round(float(sizing["size_eur"]) * risk_scale, 2)
            sizing["stop_pct"] = round(adjusted_stop_pct, 3)
            sizing["volatility_stop_adjustment"] = {
                "reason": "leveraged_etf_stop_scalar",
                "base_stop_pct": round(base_stop_pct, 3),
                "adjusted_stop_pct": round(adjusted_stop_pct, 3),
                "risk_scale": round(risk_scale, 4),
                "stop_scalar": round(stop_scalar, 3),
            }
            log_event("INFO", "volatility_stop_adjusted", {
                "ticker": ticker,
                **sizing["volatility_stop_adjustment"],
            })
    final_size = sizing["size_eur"]
    ev_size_multiplier = float(ev_result.get("size_multiplier") or 1.0)
    # Apply grade multiplier on top of EV multiplier (capped at 2.0× to prevent runaway sizing)
    if setup_grade is not None and setup_grade.size_multiplier > 0:
        combined_mult = effective_size_multiplier(setup_grade, ev_size_multiplier)
    else:
        combined_mult = ev_size_multiplier
    if setup_grade is not None and setup_grade.grade == "A+":
        atr_pct_for_quality = float(sizing.get("atr_pct") or 0)
        stop_pct_for_quality = float(sizing.get("stop_pct") or 0)
        max_atr = float(profile.get("a_plus_full_size_max_atr_pct", 2.5))
        max_stop = float(profile.get("a_plus_full_size_max_stop_pct", 5.0))
        if atr_pct_for_quality > max_atr or stop_pct_for_quality > max_stop:
            combined_mult = min(combined_mult, ev_size_multiplier)
            sizing["a_plus_size_capped"] = True
            sizing["a_plus_size_cap_reason"] = {
                "atr_pct": round(atr_pct_for_quality, 3),
                "max_atr_pct": max_atr,
                "stop_pct": round(stop_pct_for_quality, 3),
                "max_stop_pct": max_stop,
            }
            log_event("INFO", "a_plus_size_capped_by_volatility", {
                "ticker": ticker,
                **sizing["a_plus_size_cap_reason"],
            })
    if str(getattr(ticker_regime_state, "intraday_regime", "")).lower() == "ranging":
        ranging_scalar = max(0.0, min(1.0, float(profile.get("ranging_regime_size_multiplier", 0.35))))
        combined_mult *= ranging_scalar
        ranging_cap = float(profile.get("ranging_max_notional_eur", 0) or 0)
        if ranging_cap > 0:
            sizing["ranging_max_notional_eur"] = round(ranging_cap, 2)
        sizing["ranging_regime_size_scalar"] = round(ranging_scalar, 3)
        log_event("INFO", "ranging_regime_size_reduced", {
            "ticker": ticker,
            "scalar": round(ranging_scalar, 3),
            "combined_size_multiplier": round(combined_mult, 3),
        })
    final_size *= combined_mult
    if action == "SELL":
        final_size = _cap_short_notional(final_size, capital_base, profile)
    max_notional = _env_float(
        "MAX_NOTIONAL_PER_TRADE_EUR",
        profile.get("max_trade_notional_eur", final_size),
    )
    if str(getattr(ticker_regime_state, "intraday_regime", "")).lower() == "ranging":
        ranging_cap = _env_float("RANGING_MAX_NOTIONAL_EUR", float(profile.get("ranging_max_notional_eur", max_notional) or max_notional))
        if ranging_cap > 0:
            max_notional = min(max_notional, ranging_cap)
    final_size = min(final_size, max_notional)
    sizing["size_eur"] = round(final_size, 2)
    sizing["ev_decision"] = ev_result.get("ev_decision")
    sizing["ev_size_multiplier"] = round(ev_size_multiplier, 3)
    sizing["grade_size_multiplier"] = round(setup_grade.size_multiplier, 3) if setup_grade else 1.0
    sizing["combined_size_multiplier"] = round(combined_mult, 3)
    sizing["ev_result"] = ev_result
    sizing["setup_context"] = setup_context

    import yfinance as yf
    bar = yf.download(ticker, period="1d", interval="1m",
                      progress=False, auto_adjust=True)
    if bar.empty:
        log_event("WARN", "price_unavailable", {"ticker": ticker})
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "price", "price_unavailable",
            ev_result=ev_result,
        )
        return
    current_price = float(bar["Close"].squeeze().iloc[-1])
    intended_size_eur = float(final_size or 0)
    use_bracket_orders = _env_value("USE_BRACKET_ORDERS", "true").lower() != "false"

    if setup_grade is not None and setup_grade.grade in {"A+", "A"} and current_price > 0:
        min_grade_shares = _env_float("GRADE_MIN_EXECUTABLE_SHARES", 2.0)
        min_buffer = max(0.0, _env_float("GRADE_MIN_NOTIONAL_BUFFER_PCT", 0.5)) / 100
        min_notional_eur = (current_price * min_grade_shares * (1 + min_buffer)) / _eurusd_rate()
        if final_size < min_notional_eur:
            capped_min_size = min(min_notional_eur, max_notional)
            if capped_min_size > final_size:
                final_size = capped_min_size
                sizing["grade_min_executable_shares"] = min_grade_shares
                sizing["grade_min_notional_eur"] = round(min_notional_eur, 2)
                sizing["grade_min_notional_buffer_pct"] = round(min_buffer * 100, 3)
                sizing["grade_min_notional_applied"] = True
                log_event("INFO", "grade_min_notional_applied", {
                    "ticker": ticker,
                    "grade": setup_grade.grade,
                    "previous_size_eur": round(intended_size_eur, 2),
                    "new_size_eur": round(final_size, 2),
                    "min_shares": min_grade_shares,
                    "buffer_pct": round(min_buffer * 100, 3),
                    "current_price": round(current_price, 4),
                })
            else:
                sizing["grade_min_notional_applied"] = False
                sizing["grade_min_notional_capped"] = True
                sizing["grade_min_notional_eur"] = round(min_notional_eur, 2)
                sizing["grade_min_notional_buffer_pct"] = round(min_buffer * 100, 3)

    inflation_block = _probe_floor_inflation_block(
        ev_result.get("ev_decision"),
        bool(sizing.get("grade_min_notional_applied")),
        intended_size_eur,
        final_size,
        profile,
    )
    if inflation_block:
        reason = "probe_floor_inflation_block"
        log_event("INFO", reason, {
            "ticker": ticker,
            "ev_decision": ev_result.get("ev_decision"),
            "intended_size_eur": round(intended_size_eur, 2),
            "floor_size_eur": round(final_size, 2),
            **inflation_block,
        })
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "sizing", reason,
            ev_result=ev_result, reference_price=current_price,
        )
        return

    final_size_usd = _eur_to_usd(final_size)
    sizing["size_eur"] = round(final_size, 2)
    sizing["size_usd"] = round(final_size_usd, 2)
    qty = final_size_usd / current_price
    floor_qty = math.floor(qty) if use_bracket_orders else round(qty, 6)
    bracket_floor_qty_loss_pct = (
        round(max(0.0, (qty - floor_qty) / qty * 100), 4)
        if use_bracket_orders and qty > 0 else 0.0
    )
    sizing["intended_size_eur"] = round(intended_size_eur, 2)
    sizing["implied_qty"] = round(qty, 6)
    sizing["floor_qty"] = floor_qty
    sizing["bracket_floor_qty_loss_pct"] = bracket_floor_qty_loss_pct

    if use_bracket_orders and floor_qty < 1:
        reason = "bracket_floor_would_waste_trade"
        log_event("INFO", "bracket_floor_preflight_block", {
            "ticker": ticker,
            "grade": setup_grade.grade if setup_grade else None,
            "final_size_eur": round(final_size, 2),
            "size_usd": round(final_size_usd, 2),
            "current_price": round(current_price, 4),
            "implied_qty": round(qty, 6),
            "floor_qty": floor_qty,
            "ev_decision": ev_result.get("ev_decision"),
        })
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "sizing", reason,
            ev_result=ev_result, reference_price=current_price,
        )
        return

    raw_hold_minutes = llm_result.get("hold_minutes", 30)
    try:
        hold_minutes = int(raw_hold_minutes)
    except (TypeError, ValueError):
        hold_minutes = 30
    mean_reversion_trade = bool(signal_result.get("mean_reversion_signal"))
    event_risk_probe = bool(setup_context.get("event_risk_intraday_probe"))
    hold_extension = None
    if mean_reversion_trade:
        hold_minutes = 2880
    else:
        hold_minutes = max(
            int(profile.get("min_hold_minutes", 1)),
            min(int(profile.get("max_hold_minutes", 60)), hold_minutes),
        )
        if event_risk_probe:
            hold_minutes = min(
                hold_minutes,
                int(profile.get("event_risk_max_hold_minutes", 30)),
            )
        else:
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
    if event_risk_probe:
        stop_loss_pct = max(
            float(profile.get("event_risk_min_stop_pct", 0.25)),
            stop_loss_pct * float(profile.get("event_risk_stop_multiplier", 0.75)),
        )
        sizing["event_risk_intraday_only"] = True

    take_profit_pct = float(profile.get("take_profit_pct", profile["stop_loss_pct"] * 1.2))
    rr_block = _reward_risk_block(stop_loss_pct, take_profit_pct, ticker_regime, profile)
    if rr_block:
        log_event("INFO", "reward_risk_veto", {
            "ticker": ticker,
            "action": action,
            "composite": round(composite, 4),
            "regime": ticker_regime,
            **rr_block,
        })
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "reward_risk", rr_block["reason"],
            ev_result=ev_result, reference_price=current_price,
        )
        return
    sizing["take_profit_pct"] = round(take_profit_pct, 4)
    sizing["reward_risk_ratio"] = round(take_profit_pct / max(float(stop_loss_pct or 0), 0.0001), 4)

    order = submit_market_order(
        ticker       = ticker,
        side         = action.lower(),
        qty          = round(qty, 6),
        stop_loss_pct= stop_loss_pct,
        take_profit_pct= take_profit_pct,
        current_price= current_price,
    )

    if "error" in order:
        log_event("ERROR", "order_failed", {"ticker": ticker, "error": order["error"]})
        return

    submitted_qty = float(order.get("qty") or floor_qty or round(qty, 6))
    executed_size_usd = submitted_qty * current_price
    executed_size_eur = executed_size_usd / _eurusd_rate()
    sizing["submitted_qty"] = round(submitted_qty, 6)
    sizing["executed_size_usd"] = round(executed_size_usd, 2)
    sizing["executed_size_eur"] = round(executed_size_eur, 2)

    # Track open trade for exit monitoring
    if action == "BUY":
        stop_price = current_price * (1 - stop_loss_pct / 100)
        take_profit_price = current_price * (1 + take_profit_pct / 100)
    else:
        stop_price = current_price * (1 + stop_loss_pct / 100)
        take_profit_price = current_price * (1 - take_profit_pct / 100)

    exposure_direction = _exposure_direction(ticker, action)
    strategy_family = _strategy_family(
        ticker, action, ticker_regime, signal_result,
        horizon="short", mean_reversion_trade=mean_reversion_trade,
    )

    # Compute grade-differentiated partial exit target and runner ATR stop
    atr_raw = float(atr_data.get("atr_raw") or (current_price * float(atr_data.get("atr_pct", 2.5)) / 100))
    if setup_grade is not None and setup_grade.grade in {"A+", "A", "B"}:
        partial_atr_mult = 1.5 if setup_grade.grade == "A+" else 1.2
        if action == "BUY":
            partial_target_price = current_price + atr_raw * partial_atr_mult
        else:
            partial_target_price = current_price - atr_raw * partial_atr_mult
        partial_exit_pct = setup_grade.partial_exit_pct
        runner_atr_mult = setup_grade.runner_atr_multiplier
    else:
        partial_target_price = None
        partial_exit_pct = 0.5
        runner_atr_mult = 0.8

    _open_trades[ticker] = {
        "entry_time":    datetime.utcnow(),
        "entry_price":   current_price,
        "quantity":      submitted_qty,
        "submitted_qty": submitted_qty,
        "implied_qty":   round(qty, 6),
        "stop_price":    stop_price,
        "take_profit_price": take_profit_price,
        "hold_minutes":  hold_minutes,
        "hold_extension_count": 0,
        "hold_decision_json": hold_extension,
        "size_eur":      executed_size_eur,
        "size_usd":      executed_size_usd,
        "intended_size_eur": intended_size_eur,
        "executed_size_eur": executed_size_eur,
        "executed_size_usd": executed_size_usd,
        "bracket_floor_qty_loss_pct": bracket_floor_qty_loss_pct,
        "atr_pct":       sizing.get("atr_pct") or atr_data.get("atr_pct"),
        "atr_raw":       atr_data.get("atr_raw"),
        "stop_pct":      stop_loss_pct,
        "stop_multiplier": sizing.get("stop_multiplier"),
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
        "swing_trade":   False if event_risk_probe else mean_reversion_trade,
        "llm_conviction": conviction,
        "llm_rationale": llm_result.get("rationale", ""),
        "order_id":      order.get("order_id"),
        # Grade metadata
        "setup_grade":   setup_grade.grade if setup_grade else None,
        "sector_confirmation": setup_grade.sector_confirmation if setup_grade else None,
        "percentile_rank": setup_grade.percentile_rank if setup_grade else None,
        "grade_reasons": setup_grade.reasons if setup_grade else [],
        # Partial exit + runner tracking
        "partial_target_price": partial_target_price,
        "partial_exit_pct":    partial_exit_pct,
        "partial_exit_done":   False,
        "partial_exit_qty":    0.0,
        "runner_atr_mult":     runner_atr_mult,
        "runner_stop_price":   None,
        # Thesis invalidation strike counter
        "vwap_thesis_strike_count": 0,
    }
    save_open_trade(ticker, _open_trades[ticker])

    log_event("TRADE", "order_submitted", {
        "ticker": ticker, "side": action,
        "size_eur": round(executed_size_eur, 2),
        "intended_size_eur": round(intended_size_eur, 2),
        "submitted_qty": round(submitted_qty, 6),
        "implied_qty": round(qty, 6),
        "bracket_floor_qty_loss_pct": bracket_floor_qty_loss_pct,
        "conviction": conviction,
        "composite": composite, "order_class": order.get("order_class"),
        "rationale": llm_result.get("rationale"),
        "sizing": sizing,
        "mean_reversion_trade": mean_reversion_trade,
        "event_risk_intraday_probe": event_risk_probe,
        "ev_decision": ev_result.get("ev_decision"),
        "exposure_direction": exposure_direction,
        "strategy_family": strategy_family,
    })


def _process_ticker(ticker, regime, weights, profile, portfolio_state, recent_trades,
                    regime_state, shock_result):
    """Compatibility wrapper for one-off ticker processing."""
    candidate = _evaluate_ticker_candidate(
        ticker, regime, weights, profile, portfolio_state, recent_trades,
        regime_state, shock_result,
    )
    if candidate:
        _execute_trade_candidate(candidate, profile, portfolio_state)


def _check_thesis_invalidation(ticker: str, trade: dict) -> Optional[str]:
    """
    2 consecutive cycles where price is on the wrong side of VWAP kills the breakout thesis.
    Uses only the signal cache — zero extra API calls.
    OR: 1 VWAP strike + tape deterioration → immediate exit.
    """
    cached = _signal_cache.get(ticker)
    if cached is None:
        return None
    signals_now = cached[1].get("signals", {})
    vwap_val = signals_now.get("vwap_deviation") or {}
    tape_val = signals_now.get("tape_aggression") or {}
    vwap_score = float(vwap_val.get("score", 0) if isinstance(vwap_val, dict) else 0)
    tape_score = float(tape_val.get("score", 0) if isinstance(tape_val, dict) else 0)

    side = trade.get("side", "BUY")
    direction = 1 if side == "BUY" else -1

    # vwap_score > 0 = price BELOW VWAP. For a BUY breakout that's a thesis threat.
    # vwap_score < 0 = price ABOVE VWAP. For a SELL breakout that's a thesis threat.
    vwap_against = (direction == 1 and vwap_score > 0.2) or (direction == -1 and vwap_score < -0.2)
    tape_against = (tape_score * direction) < -0.2

    if vwap_against:
        count = int(_open_trades[ticker].get("vwap_thesis_strike_count", 0)) + 1
        _open_trades[ticker]["vwap_thesis_strike_count"] = count
        save_open_trade(ticker, _open_trades[ticker])
        if count >= 2:
            log_event("INFO", "thesis_invalidated_vwap_2strike", {
                "ticker": ticker, "vwap_score": round(vwap_score, 3),
                "strikes": count, "side": side,
            })
            return "thesis_invalidated"
        if tape_against:
            log_event("INFO", "thesis_invalidated_vwap_tape", {
                "ticker": ticker, "vwap_score": round(vwap_score, 3),
                "tape_score": round(tape_score, 3), "side": side,
            })
            return "thesis_invalidated"
    else:
        if _open_trades[ticker].get("vwap_thesis_strike_count", 0) != 0:
            _open_trades[ticker]["vwap_thesis_strike_count"] = 0
            save_open_trade(ticker, _open_trades[ticker])
    return None


def _check_partial_exit(ticker: str, trade: dict, current_price: float):
    """
    Execute a partial exit if the price has reached the grade-defined target.
    Updates _open_trades with runner stop after the partial.
    No-op if partial target is not set or already done.
    """
    if trade.get("partial_exit_done"):
        return

    partial_target = trade.get("partial_target_price")
    if not partial_target:
        return

    side = trade.get("side", "BUY")
    target_hit = (side == "BUY" and current_price >= partial_target) or \
                 (side == "SELL" and current_price <= partial_target)
    if not target_hit:
        return

    total_qty = float(trade.get("quantity") or 0)
    if total_qty <= 0:
        return

    partial_pct = float(trade.get("partial_exit_pct") or 0.5)
    close_qty = round(total_qty * partial_pct, 6)
    if close_qty <= 0:
        return

    cancel_results = _cancel_bracket_orders_for_manual_exit(ticker, trade)
    if cancel_results:
        log_event("INFO", "bracket_orders_cancelled_for_partial_exit", {
            "ticker": ticker,
            "results": cancel_results[:4],
        })

    close_side = "sell" if side == "BUY" else "buy"
    result = close_partial_position(ticker, close_qty, close_side)
    if result.get("error"):
        log_event("WARN", "partial_exit_failed", {
            "ticker": ticker, "close_qty": close_qty, "error": result["error"],
        })
        return

    remaining_qty = total_qty - close_qty
    runner_atr_mult = float(trade.get("runner_atr_mult") or 1.0)

    # Compute runner trailing stop from current price
    try:
        from backend.signals.engine import compute_atr as _compute_atr
        atr_info = _compute_atr(ticker)
        atr_raw = float(atr_info.get("atr_raw") or (current_price * 0.025))
    except Exception:
        atr_raw = current_price * 0.025

    if side == "BUY":
        runner_stop = current_price - atr_raw * runner_atr_mult
        stop_side = "sell"
    else:
        runner_stop = current_price + atr_raw * runner_atr_mult
        stop_side = "buy"

    stop_order = submit_stop_order(ticker, stop_side, remaining_qty, runner_stop, time_in_force="day")
    if stop_order.get("error"):
        log_event("WARN", "runner_stop_submit_failed", {
            "ticker": ticker,
            "remaining_qty": remaining_qty,
            "runner_stop": round(runner_stop, 4),
            "error": stop_order["error"],
        })

    _open_trades[ticker]["partial_exit_done"] = True
    _open_trades[ticker]["partial_exit_qty"] = close_qty
    _open_trades[ticker]["quantity"] = remaining_qty
    _open_trades[ticker]["runner_stop_price"] = round(runner_stop, 4)
    if not stop_order.get("error"):
        _open_trades[ticker]["protective_stop_order_id"] = stop_order.get("order_id")

    save_result = save_open_trade(ticker, _open_trades[ticker])
    if save_result.get("error"):
        # Partial exit executed at broker but state not persisted — next cold-start
        # will hydrate partial_exit_done=False and risk a duplicate partial exit.
        log_event("ERROR", "partial_exit_save_failed", {
            "ticker": ticker,
            "error": save_result["error"],
            "partial_exit_done": True,
            "remaining_qty": remaining_qty,
        })

    log_event("TRADE", "partial_exit_executed", {
        "ticker": ticker, "side": side, "close_qty": close_qty,
        "remaining_qty": remaining_qty, "partial_pct": partial_pct,
        "exit_price": current_price, "partial_target": round(partial_target, 4),
        "runner_stop": round(runner_stop, 4), "runner_atr_mult": runner_atr_mult,
        "order_id": result.get("order_id"),
        "runner_stop_order_id": stop_order.get("order_id"),
        "state_persisted": not bool(save_result.get("error")),
    })


def _check_exits(portfolio_state, profile):
    """Check all open trades for stop-loss, chandelier stop, or time-based exit."""
    import yfinance as yf
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC for hold_elapsed arithmetic
    now_aware = datetime.now(timezone.utc)
    eod_cleanup = _is_eod_intraday_cleanup_window(now_aware)
    eod_final_force = _is_eod_final_force_exit_window(now_aware)
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

            # Leveraged ETFs must be closed by profile's max_hold cutoff (default 3:45 PM ET)
            if exit_reason is None and _is_leveraged_etf(ticker, profile):
                max_hold_min = int(profile.get("leveraged_etf_max_hold_minutes", 345))
                ny_now = _to_new_york_time(datetime.now(timezone.utc))
                market_open_minutes = ny_now.hour * 60 + ny_now.minute - (9 * 60 + 30)
                if market_open_minutes >= max_hold_min:
                    exit_reason = "leveraged_etf_time_exit"
                    log_event("INFO", "leveraged_etf_time_exit", {
                        "ticker": ticker,
                        "market_open_minutes": market_open_minutes,
                        "max_hold_minutes": max_hold_min,
                    })

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

                # Partial exit + runner check (before stop/TP — runner may have its own stop)
                if exit_reason is None and not trade.get("partial_exit_done"):
                    _check_partial_exit(ticker, trade, current_price)

                # Thesis invalidation: 2 consecutive VWAP-against closes
                if exit_reason is None and not trade.get("mean_reversion_trade"):
                    exit_reason = _check_thesis_invalidation(ticker, trade)

                # Stop-loss and take-profit always evaluated first
                # If runner is active, use runner_stop_price in place of original stop
                stop_price = float(trade.get("runner_stop_price") or 0) or trade["stop_price"]
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
                        # Near close: promote/carry strong swing-eligible trades, close everything else.
                        if eod_final_force:
                            pnl_pct_eod = _trade_pnl_pct(trade, current_price)
                            log_event("INFO", "eod_intraday_cleanup_exit", {
                                "ticker": ticker,
                                "pnl_pct": round(pnl_pct_eod, 4),
                                "outcome": "loser" if pnl_pct_eod < 0 else (
                                    "flat" if pnl_pct_eod < 0.05 else "winner_final_window"
                                ),
                                "eod_decision": "final_window_force_exit",
                                "minutes_to_close": _minutes_to_regular_close(now_aware),
                            })
                            exit_reason = "eod_cleanup"
                        else:
                            # Regime is cached per-call to avoid duplicate detect_regime fetches.
                            cached_regime = _regime_cache.get(ticker)
                            if cached_regime is None:
                                cached_regime = detect_regime(ticker)
                                _regime_cache[ticker] = cached_regime
                            if _try_promote_to_swing(ticker, trade, current_price, profile, cached_regime):
                                eod_decision = (
                                    "carry_overnight"
                                    if _trade_pnl_pct(trade, current_price) <= 0
                                    else "promote_swing"
                                )
                                log_event("INFO", "eod_intraday_promoted", {
                                    "ticker": ticker,
                                    "pnl_pct": round(_trade_pnl_pct(trade, current_price), 4),
                                    "eod_decision": eod_decision,
                                })
                                continue
                            pnl_pct_eod = _trade_pnl_pct(trade, current_price)
                            log_event("INFO", "eod_intraday_cleanup_exit", {
                                "ticker": ticker,
                                "pnl_pct": round(pnl_pct_eod, 4),
                                "outcome": "loser" if pnl_pct_eod < 0 else (
                                    "flat" if pnl_pct_eod < 0.05 else "winner_not_swing_qualified"
                                ),
                                "eod_decision": "force_exit",
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


def _cancel_bracket_orders_for_manual_exit(ticker: str, trade: dict) -> list[dict]:
    """
    Time exits can conflict with open bracket legs that reserve shares.
    Cancel those legs first, then submit the manual close.
    """
    order_id = trade.get("order_id")
    results = []

    if order_id:
        order = get_order_by_id(str(order_id))
        if "error" in order:
            results.append({"order_id": order_id, "error": order["error"]})
        else:
            terminal = {"filled", "canceled", "cancelled", "expired", "rejected"}
            for leg in order.get("legs", []):
                status = str(leg.get("status", "")).lower()
                leg_id = leg.get("id")
                if not leg_id or any(state in status for state in terminal):
                    continue
                results.append(cancel_order_by_id(str(leg_id)))

    if results:
        time.sleep(1)
    symbol_results = cancel_open_orders_for_symbol(ticker)
    if symbol_results:
        results.extend(symbol_results)
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

    cancel_results = _cancel_bracket_orders_for_manual_exit(ticker, trade)
    if cancel_results:
        log_event("INFO", "open_orders_cancelled_for_exit", {
            "ticker": ticker,
            "exit_reason": exit_reason,
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

    size_eur    = float(trade.get("executed_size_eur") or trade.get("size_eur") or 0)
    size_usd    = float(trade.get("executed_size_usd") or trade.get("size_usd") or _eur_to_usd(size_eur))
    if size_eur <= 0:
        size_eur = float(trade.get("intended_size_eur") or 0)
        size_usd = _eur_to_usd(size_eur)
    if size_eur <= 0:
        log_event("WARN", "trade_close_missing_exposure", {
            "ticker": ticker,
            "exit_reason": exit_reason,
        })
        size_eur = 1.0
        size_usd = _eur_to_usd(size_eur)
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
        "submitted_qty":   trade.get("submitted_qty") or trade.get("quantity"),
        "implied_qty":     trade.get("implied_qty"),
        "stop_price":      round(float(trade.get("stop_price") or 0), 4),
        "take_profit_price": round(float(trade.get("take_profit_price") or 0), 4),
        "size_eur":        round(size_eur, 2),
        "size_usd":        round(size_usd, 2),
        "intended_size_eur": round(float(trade.get("intended_size_eur") or size_eur), 2),
        "executed_size_eur": round(size_eur, 2),
        "executed_size_usd": round(size_usd, 2),
        "bracket_floor_qty_loss_pct": trade.get("bracket_floor_qty_loss_pct"),
        "pnl_pct":         round(pnl_pct, 4),
        "net_pnl_pct":     round(net_pnl_pct, 4),
        "pnl_eur":         round(net_pnl_pct / 100 * size_eur, 2),
        "entry_time":      trade["entry_time"].isoformat() + "Z",
        "exit_time":       now_close.isoformat() + "Z",
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
        "setup_grade":     trade.get("setup_grade"),
        "partial_exit_done": bool(trade.get("partial_exit_done")),
        "entry_tranche_count": int(trade.get("entry_tranche_count") or 1),
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
    intended_size_eur = float(size_eur or 0)
    sizing["size_eur"] = round(size_eur, 2)

    current_price = _current_daily_price(ticker)
    if not current_price:
        log_event("WARN", "price_unavailable", {"ticker": ticker, "horizon": horizon})
        return {"error": "price_unavailable"}

    size_usd = _eur_to_usd(size_eur)
    sizing["size_usd"] = round(size_usd, 2)
    qty = size_usd / current_price
    use_bracket_orders = _env_value("USE_BRACKET_ORDERS", "true").lower() != "false"
    floor_qty = math.floor(qty) if use_bracket_orders else round(qty, 6)
    bracket_floor_qty_loss_pct = (
        round(max(0.0, (qty - floor_qty) / qty * 100), 4)
        if use_bracket_orders and qty > 0 else 0.0
    )
    sizing["intended_size_eur"] = round(intended_size_eur, 2)
    sizing["implied_qty"] = round(qty, 6)
    sizing["floor_qty"] = floor_qty
    sizing["bracket_floor_qty_loss_pct"] = bracket_floor_qty_loss_pct
    if use_bracket_orders and floor_qty < 1:
        log_event("INFO", "bracket_floor_preflight_block", {
            "ticker": ticker,
            "horizon": horizon,
            "size_eur": round(size_eur, 2),
            "size_usd": round(size_usd, 2),
            "current_price": round(current_price, 4),
            "implied_qty": round(qty, 6),
            "floor_qty": floor_qty,
            "reason": "bracket_floor_would_waste_trade",
        })
        return {"error": "bracket_floor_would_waste_trade", "ticker": ticker}
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

    submitted_qty = float(order.get("qty") or floor_qty or round(qty, 6))
    executed_size_usd = submitted_qty * current_price
    executed_size_eur = executed_size_usd / _eurusd_rate()
    sizing["submitted_qty"] = round(submitted_qty, 6)
    sizing["executed_size_usd"] = round(executed_size_usd, 2)
    sizing["executed_size_eur"] = round(executed_size_eur, 2)

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
        "quantity": submitted_qty,
        "submitted_qty": submitted_qty,
        "implied_qty": round(qty, 6),
        "stop_price": stop_price,
        "take_profit_price": take_profit_price,
        "hold_minutes": hold_minutes or 0,
        "hold_days": hold_days or 0,
        "size_eur": executed_size_eur,
        "size_usd": executed_size_usd,
        "intended_size_eur": intended_size_eur,
        "executed_size_eur": executed_size_eur,
        "executed_size_usd": executed_size_usd,
        "bracket_floor_qty_loss_pct": bracket_floor_qty_loss_pct,
        "atr_pct": sizing.get("atr_pct") or (atr_data or {}).get("atr_pct"),
        "atr_raw": (atr_data or {}).get("atr_raw"),
        "stop_pct": stop_loss_pct,
        "stop_multiplier": sizing.get("stop_multiplier"),
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
        "size_eur": round(executed_size_eur, 2),
        "intended_size_eur": round(intended_size_eur, 2),
        "submitted_qty": round(submitted_qty, 6),
        "implied_qty": round(qty, 6),
        "bracket_floor_qty_loss_pct": bracket_floor_qty_loss_pct,
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
    portfolio_state = _get_portfolio_state()
    _hydrate_open_trades(portfolio_state.get("positions", []))
    open_swings = [
        t for t, data in _open_trades.items()
        if data.get("promoted_to_swing") is True
    ]

    if not open_swings:
        log_event("INFO", "swing_reeval_no_positions", {})
        return

    log_event("INFO", "swing_reeval_start", {"positions": open_swings})

    profile = _apply_execution_overrides(
        get_effective_profile(PROFILE, portfolio_state)
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

    _hydrate_open_trades(portfolio_state.get("positions", []))
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
    # Fetch up to 365 snapshots (ordered DESC) to derive both daily and
    # cumulative P&L from actual history rather than an arbitrary EUR→USD
    # conversion that produces nonsense numbers when paper equity >> start capital.
    snaps = get_snapshots(days=365)
    equity = portfolio_state["equity"]

    fx_rate = float(os.getenv("EURUSD_RATE", "1.08") or "1.08")
    unrealized_pnl_usd = float(portfolio_state.get("unrealized_pnl_usd") or 0)
    effective_total_eur = (equity + unrealized_pnl_usd) / fx_rate
    effective_cash_eur = float(portfolio_state.get("cash") or 0) / fx_rate

    # daily_pnl_pct: change since the previous snapshot
    prev_equity = snaps[0]["total_value_eur"] if snaps else None
    if prev_equity and prev_equity > 0:
        daily_pnl_pct = round((effective_total_eur - prev_equity) / prev_equity * 100, 3)
        daily_pnl_pct = max(-9999.0, min(9999.0, daily_pnl_pct))
    else:
        daily_pnl_pct = 0.0

    # cumulative_pnl_pct: change since the oldest available snapshot
    oldest_equity = snaps[-1]["total_value_eur"] if snaps else None
    if oldest_equity and oldest_equity > 0:
        cum_pnl = round((effective_total_eur - oldest_equity) / oldest_equity * 100, 3)
        cum_pnl = max(-9999.0, min(9999.0, cum_pnl))
    else:
        cum_pnl = 0.0

    save_snapshot({
        "total_value_eur":    round(effective_total_eur, 2),
        "cash_eur":           round(effective_cash_eur, 2),
        "daily_pnl_pct":      daily_pnl_pct,
        "cumulative_pnl_pct": cum_pnl,
        "drawdown_pct":       portfolio_state["drawdown_today"],
        "open_positions":     portfolio_state["positions"],
        "trades_today":       portfolio_state["trades_today"],
        "llm_calls_today":    _llm_calls_this_hour,
        "llm_cost_today":     round(_llm_calls_this_hour * 0.001, 4),
        "broker_equity_usd":  portfolio_state.get("broker_equity_usd"),
        "broker_cash_usd":    portfolio_state.get("broker_cash_usd"),
        "effective_equity_usd": portfolio_state.get("equity"),
        "effective_cash_usd": portfolio_state.get("cash"),
        "open_market_value_usd": portfolio_state.get("net_market_value_usd"),
        "gross_market_value_usd": portfolio_state.get("gross_market_value_usd"),
        "unrealized_pnl_usd": unrealized_pnl_usd,
        "unrealized_pnl_eur": round(unrealized_pnl_usd / fx_rate, 2),
        "fx_rate": fx_rate,
        "capital_ceiling_eur": portfolio_state.get("capital_ceiling_eur"),
        "capital_ceiling_usd": portfolio_state.get("capital_ceiling_usd"),
    })


# ── Nightly sweep (after US market close) ────────────────────────────────────

def run_nightly_sweep():
    """Runs after US market close every weekday. Simulation on alpaca_paper, live on ibkr_live."""
    try:
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
            _send_discord_alert(
                f"💰 Sweep simulation: Would park "
                f"€{plan['sweepable_eur']:.0f} in {plan['sweep_ticker']}. "
                f"Est. daily yield: €{plan['est_daily_yield']:.2f}"
            )
    except Exception as e:
        log_event("ERROR", "nightly_sweep_failed", {"error": str(e)[:100]})


# ── Post-market analytics (runs after market close, Mon–Fri) ─────────────────

def run_post_market_analytics():
    """
    Runs after US market close (21:05 UTC / 5:05 PM ET).
    Replays blocked opportunities and closed trade exits against post-event
    price action. Kept out of the signal cycle to avoid I/O overhead.
    """
    try:
        log_event("INFO", "post_market_analytics_start", {})
        _replay_blocked_opportunities()
        _replay_closed_trade_exits()
        log_event("INFO", "post_market_analytics_complete", {})
    except Exception as e:
        log_event("ERROR", "post_market_analytics_failed", {"error": str(e)[:160]})


def run_daily_eod_review():
    """Run read-only daily post-market synthesis and recommendations."""
    try:
        from backend.daily_review import run_daily_eod_review as _review
        return _review()
    except Exception as e:
        log_event("ERROR", "daily_eod_review_failed", {"error": str(e)[:160]})
        return {"error": str(e)}


# ── Weekly portfolio review (advisory, observation only) ─────────────────────

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


# ── Weekly digest (called by scheduler) ──────────────────────────────────────

def run_weekly_digest():
    from database.client import get_recent_trades, get_daily_reviews, save_learning
    trades = get_recent_trades(days=7)
    daily_reviews = get_daily_reviews(limit=7)
    if not trades and not daily_reviews:
        return
    insights = generate_weekly_insights(trades, daily_reviews=daily_reviews)
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

    # Weekly portfolio review: Sunday 17:00 UTC (one hour before digest)
    scheduler.add_job(run_portfolio_review, "cron",
                      day_of_week="sun", hour=17, minute=0,
                      timezone=timezone.utc)

    # Weekly digest: Sunday 18:00 UTC
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
    elif mode == "portfolio_review":
        run_portfolio_review()
    elif mode == "signal":
        run_signal_cycle()
    elif mode == "swing_reeval":
        re_evaluate_swing_positions()
    else:
        start_scheduler()
