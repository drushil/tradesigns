"""
Manual trading advisory mode.

This module reuses the signal engine but never submits broker orders. It logs
high-conviction suggestions to advisory_signals and sends live US trade cards
to Discord while EU runs in shadow/observation mode by default.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from config.risk_profiles import get_profile
from backend.signals.engine import compute_all_signals, detect_regime, news_sentiment_score
try:
    from backend.signals.engine import _get_bars
except ImportError:  # tests may stub backend.signals.engine without private helpers
    _get_bars = None
from backend.execution.gates import _late_chase_block
from backend.learning.engine import compute_expected_value
from database.client import (
    get_recent_trades,
    get_recent_advisory_signals,
    get_open_advisory_positions,
    get_fx_rate_cache,
    insert_advisory_signal,
    log_event,
    upsert_fx_rate_cache,
    update_advisory_exit_status,
    insert_advisory_scan_log,
    create_virtual_position,
    get_open_virtual_positions,
    update_virtual_position,
)


ADVISORY_UNIVERSE = {
    # Field guide:
    #   category       — grouping for prioritisation and filtering
    #   priority       — "high" | "medium" | "low"; high tickers are scanned first
    #   trade_target   — False suppresses Discord trade cards (still computed for regime context)
    #   benchmark_only — True means index/ETF used for market context only, never a trade alert
    #   broker_tags    — platforms where this ticker can realistically be executed from Germany
    #   liquidity_note — optional one-liner appended to the Discord card (thin markets etc.)
    "US": [
        # --- Core momentum names (high priority, Trade Republic DE / Scalable DE) ---
        {"data_symbol": "NVDA", "broker_display_name": "NVIDIA", "exchange": "NASDAQ", "currency": "USD",
         "category": "semis", "priority": "high", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "AMD", "broker_display_name": "AMD", "exchange": "NASDAQ", "currency": "USD",
         "category": "semis", "priority": "high", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "AAPL", "broker_display_name": "Apple", "exchange": "NASDAQ", "currency": "USD",
         "category": "mega_tech", "priority": "high", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "MSFT", "broker_display_name": "Microsoft", "exchange": "NASDAQ", "currency": "USD",
         "category": "mega_tech", "priority": "high", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "META", "broker_display_name": "Meta Platforms", "exchange": "NASDAQ", "currency": "USD",
         "category": "mega_tech", "priority": "high", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "AMZN", "broker_display_name": "Amazon", "exchange": "NASDAQ", "currency": "USD",
         "category": "mega_tech", "priority": "high", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "TSLA", "broker_display_name": "Tesla", "exchange": "NASDAQ", "currency": "USD",
         "category": "ev_auto", "priority": "high", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        # --- Expansion: diversification beyond mega-cap tech ---
        {"data_symbol": "GOOGL", "broker_display_name": "Alphabet", "exchange": "NASDAQ", "currency": "USD",
         "category": "mega_tech", "priority": "high", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "NFLX", "broker_display_name": "Netflix", "exchange": "NASDAQ", "currency": "USD",
         "category": "streaming", "priority": "medium", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "PLTR", "broker_display_name": "Palantir", "exchange": "NASDAQ", "currency": "USD",
         "category": "ai_defence", "priority": "high", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "AVGO", "broker_display_name": "Broadcom", "exchange": "NASDAQ", "currency": "USD",
         "category": "semis", "priority": "medium", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "MU", "broker_display_name": "Micron Technology", "exchange": "NASDAQ", "currency": "USD",
         "category": "semis", "priority": "medium", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        # --- Benchmark/context only: computed for regime signals, no trade alerts ---
        {"data_symbol": "QQQ", "broker_display_name": "Invesco QQQ", "exchange": "NASDAQ", "currency": "USD",
         "category": "etf_benchmark", "priority": "low", "trade_target": False, "benchmark_only": True,
         "broker_tags": []},
        {"data_symbol": "SPY", "broker_display_name": "SPDR S&P 500 ETF", "exchange": "NYSE Arca", "currency": "USD",
         "category": "etf_benchmark", "priority": "low", "trade_target": False, "benchmark_only": True,
         "broker_tags": []},
    ],
    "EU": [
        # --- Native EU names (shadow mode; promoted to live when EU advisory goes live) ---
        {"data_symbol": "ASML.AS", "broker_display_name": "ASML", "exchange": "Euronext Amsterdam", "currency": "EUR",
         "category": "semis_equipment", "priority": "high", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "ASM.AS", "broker_display_name": "ASM International", "exchange": "Euronext Amsterdam", "currency": "EUR",
         "category": "semis_equipment", "priority": "high", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"],
         "liquidity_note": "use limit orders — spread widens at open and close"},
        {"data_symbol": "BESI.AS", "broker_display_name": "BE Semiconductor", "exchange": "Euronext Amsterdam", "currency": "EUR",
         "category": "semis_equipment", "priority": "high", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"],
         "liquidity_note": "high beta — spread can be wide; use limit orders"},
        {"data_symbol": "SAP.DE", "broker_display_name": "SAP", "exchange": "Xetra", "currency": "EUR",
         "category": "enterprise_software", "priority": "high", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "SIE.DE", "broker_display_name": "Siemens", "exchange": "Xetra", "currency": "EUR",
         "category": "industrial_tech", "priority": "medium", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "RHM.DE", "broker_display_name": "Rheinmetall", "exchange": "Xetra", "currency": "EUR",
         "category": "defence", "priority": "high", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "ENR.DE", "broker_display_name": "Siemens Energy", "exchange": "Xetra", "currency": "EUR",
         "category": "energy_transition", "priority": "high", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "AIR.PA", "broker_display_name": "Airbus", "exchange": "Euronext Paris", "currency": "EUR",
         "category": "aerospace_defence", "priority": "high", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "MC.PA", "broker_display_name": "LVMH", "exchange": "Euronext Paris", "currency": "EUR",
         "category": "luxury", "priority": "medium", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "ALV.DE", "broker_display_name": "Allianz", "exchange": "Xetra", "currency": "EUR",
         "category": "insurance", "priority": "medium", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "IFX.DE", "broker_display_name": "Infineon", "exchange": "Xetra", "currency": "EUR",
         "category": "semis", "priority": "medium", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "DBK.DE", "broker_display_name": "Deutsche Bank", "exchange": "Xetra", "currency": "EUR",
         "category": "financials", "priority": "medium", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        # --- EU mirrors: early-read on US momentum via TR morning watch (L&S) + eu_open (Xetra) ---
        {"data_symbol": "NVD.DE", "broker_display_name": "NVIDIA (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "NVDA",
         "mirror_only_windows": ["tr_morning_watch", "eu_open"],
         "category": "eu_mirror", "priority": "medium", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "AMD.DE", "broker_display_name": "AMD (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "AMD",
         "mirror_only_windows": ["tr_morning_watch", "eu_open"],
         "category": "eu_mirror", "priority": "medium", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "APC.DE", "broker_display_name": "Apple (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "AAPL",
         "mirror_only_windows": ["tr_morning_watch", "eu_open"],
         "category": "eu_mirror", "priority": "medium", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "MSF.DE", "broker_display_name": "Microsoft (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "MSFT",
         "mirror_only_windows": ["tr_morning_watch", "eu_open"],
         "category": "eu_mirror", "priority": "medium", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "AMZ.DE", "broker_display_name": "Amazon (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "AMZN",
         "mirror_only_windows": ["tr_morning_watch", "eu_open"],
         "category": "eu_mirror", "priority": "medium", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "TL0.DE", "broker_display_name": "Tesla (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "TSLA",
         "mirror_only_windows": ["tr_morning_watch", "eu_open"],
         "category": "eu_mirror", "priority": "medium", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "FB2A.DE", "broker_display_name": "Meta (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "META",
         "mirror_only_windows": ["tr_morning_watch", "eu_open"],
         "category": "eu_mirror", "priority": "medium", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "ABEA.DE", "broker_display_name": "Alphabet A (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "GOOGL",
         "mirror_only_windows": ["tr_morning_watch", "eu_open"],
         "category": "eu_mirror", "priority": "medium", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "PTX.DE", "broker_display_name": "Palantir (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "PLTR",
         "mirror_only_windows": ["tr_morning_watch", "eu_open"],
         "category": "eu_mirror", "priority": "medium", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "NFC.DE", "broker_display_name": "Netflix (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "NFLX",
         "mirror_only_windows": ["tr_morning_watch", "eu_open"],
         "category": "eu_mirror", "priority": "medium", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de", "scalable_de"]},
        {"data_symbol": "AVG.DE", "broker_display_name": "Broadcom (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "AVGO",
         "mirror_only_windows": ["tr_morning_watch", "eu_open"],
         "category": "eu_mirror", "priority": "medium", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de"]},
        {"data_symbol": "MTE.DE", "broker_display_name": "Micron (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "MU",
         "mirror_only_windows": ["tr_morning_watch", "eu_open"],
         "category": "eu_mirror", "priority": "medium", "trade_target": True, "benchmark_only": False,
         "broker_tags": ["trade_republic_de"]},
    ],
}

EU_WEIGHTS = {
    "order_book_imbalance": 0.00,
    "tape_aggression": 0.08,
    "rsi_divergence": 0.07,
    "news_sentiment": 0.05,
    "vwap_deviation": 0.28,
    "macd_crossover": 0.18,
    "relative_strength": 0.14,
    "bollinger_squeeze": 0.07,
    "put_call_ratio": 0.00,
}

EU_MIRROR_WEIGHTS = {
    "order_book_imbalance": 0.00,
    "tape_aggression": 0.10,
    "rsi_divergence": 0.08,
    "news_sentiment": 0.18,
    "vwap_deviation": 0.14,
    "macd_crossover": 0.25,
    "relative_strength": 0.20,
    "bollinger_squeeze": 0.05,
    "put_call_ratio": 0.00,
}

LIVE_SIGNAL_STATUSES = {"sent", "entered", "hit_stop", "hit_target"}
OPEN_LIVE_STATUSES = {"sent", "entered"}
WATCH_SIGNAL_STATUSES = {"skipped"}
WATCH_ALERT_STAGES = {"watch", "ignition", "tr_morning_watch"}
ALERT_STAGE_RANK = {"ignition": 0, "watch": 1, "tr_morning_watch": 1, "trade": 2}


@dataclass
class AdvisoryConfig:
    markets: set[str]
    live_markets: set[str]
    shadow_markets: set[str]
    shadow_discord_markets: set[str]
    capital_eur: float
    max_live_alerts_per_day: int
    max_shadow_signals_per_day: int
    max_shadow_discord_alerts_per_day: int
    max_open_live_trades: int
    max_live_trades_per_session: int
    risk_per_trade_eur: float
    max_daily_loss_eur: float
    default_size_eur: float
    a_plus_max_size_eur: float
    min_composite: float
    min_watch_composite: float
    min_watch_breakout_quality: float
    min_ev_pct: float
    min_breakout_quality: float
    min_discord_grade: str
    shadow_min_discord_grade: str
    us_min_minutes_after_open: int
    allow_short: bool
    discord_webhook_url: str
    fx_rate: float
    fx_rate_source: str = "unknown"
    fx_rate_fetched_at: str = ""


def _env_value(key: str, default: str) -> str:
    value = os.getenv(key)
    return default if value is None or not value.strip() else value.strip()


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env_value(key, str(default)))
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env_value(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _today_utc_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _latest_close_from_bars(bars) -> Optional[float]:
    """Extract a latest close from normal or yfinance MultiIndex OHLCV frames."""
    if bars is None or getattr(bars, "empty", True):
        return None
    try:
        close = bars["Close"]
    except Exception:
        try:
            close = bars.xs("Close", axis=1, level=0)
        except Exception:
            return None
    try:
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]
        if hasattr(close, "squeeze"):
            close = close.squeeze()
        if hasattr(close, "dropna"):
            clean = close.dropna()
            if len(clean) == 0:
                return None
            rate = float(clean.iloc[-1])
        else:
            rate = float(close)
    except Exception:
        return None
    return rate if 0.8 <= rate <= 1.4 else None


def _fetch_latest_eurusd_rate() -> Optional[float]:
    """Fetch EUR/USD once through the existing bars data path."""
    bars = None
    try:
        if _get_bars is not None:
            bars = _get_bars("EURUSD=X", period="5d", interval="1d")
        rate = _latest_close_from_bars(bars)
        if rate is not None:
            return rate
    except Exception:
        pass
    try:
        import yfinance as yf
        bars = yf.download("EURUSD=X", period="5d", interval="1d", progress=False, auto_adjust=True)
        return _latest_close_from_bars(bars)
    except Exception:
        return None
    return None


def _resolve_daily_fx_rate(pair: str = "EURUSD") -> dict:
    """Resolve one daily EUR/USD rate inside the advisory cycle, with DB cache."""
    today = _today_utc_date()
    cached_today = get_fx_rate_cache(pair, rate_date=today)
    if cached_today and cached_today.get("rate"):
        return {
            "rate": float(cached_today["rate"]),
            "source": cached_today.get("source") or "daily_cache",
            "fetched_at": cached_today.get("fetched_at") or "",
        }

    fetched = _fetch_latest_eurusd_rate()
    if fetched:
        saved = upsert_fx_rate_cache(
            pair,
            fetched,
            source="yfinance_daily",
            rate_date=today,
            meta={"symbol": "EURUSD=X"},
        )
        return {
            "rate": float(fetched),
            "source": "yfinance_daily" if "error" not in saved else "yfinance_daily_uncached",
            "fetched_at": saved.get("fetched_at") or datetime.now(timezone.utc).isoformat(),
        }

    cached_recent = get_fx_rate_cache(pair, max_age_days=_env_int("ADVISORY_FX_MAX_STALE_DAYS", 7))
    if cached_recent and cached_recent.get("rate"):
        return {
            "rate": float(cached_recent["rate"]),
            "source": f"stale_cache:{cached_recent.get('rate_date')}",
            "fetched_at": cached_recent.get("fetched_at") or "",
        }

    # Final fallback: explicitly-set EURUSD_RATE env var. Guards against the
    # first-run-with-yfinance-offline case where DB cache is empty. Only honoured
    # when the value is plausible (0.8–1.4); otherwise we surface "unavailable"
    # so the issue is visible in stored cards rather than silently mispriced.
    env_raw = os.getenv("EURUSD_RATE", "").strip()
    if env_raw:
        try:
            env_rate = float(env_raw)
            if 0.8 <= env_rate <= 1.4:
                return {
                    "rate": env_rate,
                    "source": "env_fallback",
                    "fetched_at": "",
                }
        except ValueError:
            pass

    return {
        "rate": 0.0,
        "source": "unavailable",
        "fetched_at": "",
    }


def load_config() -> AdvisoryConfig:
    markets = _csv_set("ADVISORY_MARKETS", "US,EU")
    live = _csv_set("ADVISORY_LIVE_MARKETS", "US")
    shadow = _csv_set("ADVISORY_SHADOW_MARKETS", "EU")
    shadow_discord = _csv_set("ADVISORY_SHADOW_DISCORD_MARKETS", "OFF")
    fx = _resolve_daily_fx_rate()
    return AdvisoryConfig(
        markets=markets,
        live_markets=live,
        shadow_markets=shadow,
        shadow_discord_markets=shadow_discord,
        capital_eur=_env_float("ADVISORY_CAPITAL_EUR", 5000.0),
        max_live_alerts_per_day=_env_int("ADVISORY_MAX_LIVE_ALERTS_PER_DAY", 3),
        max_shadow_signals_per_day=_env_int("ADVISORY_MAX_SHADOW_SIGNALS_PER_DAY", 10),
        max_shadow_discord_alerts_per_day=_env_int("ADVISORY_MAX_SHADOW_DISCORD_ALERTS_PER_DAY", 1),
        max_open_live_trades=_env_int("ADVISORY_MAX_OPEN_LIVE_TRADES", 1),
        max_live_trades_per_session=_env_int("ADVISORY_MAX_LIVE_TRADES_PER_SESSION", 2),
        risk_per_trade_eur=_env_float("ADVISORY_RISK_PER_TRADE_EUR", 50.0),
        max_daily_loss_eur=_env_float("ADVISORY_MAX_DAILY_LOSS_EUR", 150.0),
        default_size_eur=_env_float("ADVISORY_DEFAULT_SIZE_EUR", 750.0),
        a_plus_max_size_eur=_env_float("ADVISORY_A_PLUS_MAX_SIZE_EUR", 1500.0),
        min_composite=_env_float("ADVISORY_MIN_COMPOSITE", 0.45),
        min_watch_composite=_env_float("ADVISORY_MIN_WATCH_COMPOSITE", 0.25),
        min_watch_breakout_quality=_env_float("ADVISORY_MIN_WATCH_BREAKOUT_QUALITY", 0.30),
        min_ev_pct=_env_float("ADVISORY_MIN_EV_PCT", 0.50),
        min_breakout_quality=_env_float("ADVISORY_MIN_BREAKOUT_QUALITY", 0.45),
        min_discord_grade=_env_value("ADVISORY_DISCORD_MIN_GRADE", "A").upper(),
        shadow_min_discord_grade=_env_value(
            "ADVISORY_SHADOW_DISCORD_MIN_GRADE",
            _env_value("ADVISORY_DISCORD_MIN_GRADE", "A"),
        ).upper(),
        us_min_minutes_after_open=_env_int("ADVISORY_US_MIN_MINUTES_AFTER_OPEN", 15),
        allow_short=_env_bool("ADVISORY_ALLOW_SHORT", False),
        discord_webhook_url=_env_value("DISCORD_WEBHOOK_URL", ""),
        fx_rate=fx["rate"],
        fx_rate_source=fx["source"],
        fx_rate_fetched_at=fx.get("fetched_at", ""),
    )


def _csv_set(key: str, default: str) -> set[str]:
    return {x.strip().upper() for x in _env_value(key, default).split(",") if x.strip()}


def _now_cet() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Berlin"))
    except Exception:
        return datetime.now(timezone.utc) + timedelta(hours=1)


def _window_name(market: str, now_cet: Optional[datetime] = None) -> Optional[str]:
    now_cet = now_cet or _now_cet()
    minutes = now_cet.hour * 60 + now_cet.minute
    if market == "EU":
        if 7 * 60 + 30 <= minutes < 9 * 60 + 15:
            return "tr_morning_watch"
        if 9 * 60 + 15 <= minutes <= 11 * 60:
            return "eu_open"
        if 14 * 60 <= minutes <= 16 * 60:
            return "eu_catalyst_only"
        return None
    if market == "US":
        if 15 * 60 <= minutes < 15 * 60 + 30:
            return "us_premarket"
        if 15 * 60 + 30 <= minutes < 17 * 60:
            return "us_open"
        if 17 * 60 <= minutes < 20 * 60:
            return "us_midday"
        if 20 * 60 <= minutes < 21 * 60:
            return "us_power_hour"
        if 21 * 60 <= minutes < 22 * 60:
            return "us_close"
        return None
    return None


def _session_start_cet(market: str, window: str, now_cet: datetime) -> datetime:
    starts = {
        "EU": {
            "tr_morning_watch": (7, 30),
            "eu_open":          (9, 15),
            "eu_catalyst_only": (14, 0),
        },
        "US": {
            "us_premarket":   (15, 0),
            "us_open":        (15, 30),
            "us_midday":      (17, 0),
            "us_power_hour":  (20, 0),
            "us_close":       (21, 0),
        },
    }
    hour, minute = starts.get(market, {}).get(window, (now_cet.hour, now_cet.minute))
    return now_cet.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _minutes_since_session_start(market: str, window: str, now_cet: datetime) -> float:
    start = _session_start_cet(market, window, now_cet)
    return (now_cet - start).total_seconds() / 60


def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_expired_signal(signal: dict, now_utc: datetime) -> bool:
    valid_until = _parse_dt(signal.get("valid_until"))
    return bool(valid_until and valid_until < now_utc)


def _is_open_live_signal(signal: dict, now_utc: datetime) -> bool:
    status = str(signal.get("status"))
    if status == "entered":
        return True
    if status == "sent":
        return not _is_expired_signal(signal, now_utc)
    return False


def _recent_watch_signal_in_session(recent_live: list, symbol: str, market: str,
                                    now_cet: datetime, minutes: int = 45) -> Optional[dict]:
    # get_recent_advisory_signals returns newest first; escalation compares
    # against the most recent watch state in this window.
    window = _window_name(market, now_cet)
    if not window:
        return None
    cutoff_utc = now_cet.astimezone(timezone.utc) - timedelta(minutes=minutes)
    symbol = symbol.upper()
    for signal in recent_live:
        if str(signal.get("market", "")).upper() != market:
            continue
        if str(signal.get("data_symbol", "")).upper() != symbol:
            continue
        if str(signal.get("status")) not in WATCH_SIGNAL_STATUSES:
            continue
        created_at = _parse_dt(signal.get("created_at"))
        if created_at and created_at >= cutoff_utc:
            return signal
    return None


def _watch_signal_counts_in_session(recent_live: list, now_cet: datetime) -> tuple[dict, int]:
    counts = {}
    total = 0
    for signal in recent_live:
        if str(signal.get("status")) not in WATCH_SIGNAL_STATUSES:
            continue
        if not _is_signal_in_current_session(signal, now_cet):
            continue
        market = str(signal.get("market", "")).upper()
        symbol = str(signal.get("data_symbol", "")).upper()
        if not market or not symbol:
            continue
        key = (market, symbol)
        counts[key] = counts.get(key, 0) + 1
        total += 1
    return counts, total


def _watch_had_late_chase(signal: Optional[dict]) -> bool:
    if not signal:
        return False
    signal_json = signal.get("signal_json") or {}
    return bool(signal.get("late_chase_json") or signal_json.get("late_chase"))


def _prior_runner_signal(recent_live: list, candidate: dict, now_cet: datetime) -> Optional[dict]:
    """Return a same-session B+ watch/trade signal that can justify runner context."""
    symbol = str(candidate.get("data_symbol") or "").upper()
    market = str(candidate.get("market") or "").upper()
    side = str(candidate.get("side") or "").upper()
    if not symbol or not market or not side:
        return None
    for signal in recent_live or []:
        if str(signal.get("market", "")).upper() != market:
            continue
        if str(signal.get("data_symbol", "")).upper() != symbol:
            continue
        if str(signal.get("side", "")).upper() != side:
            continue
        if not _is_signal_in_current_session(signal, now_cet):
            continue
        signal_json = signal.get("signal_json") or {}
        stage = str(signal_json.get("alert_stage") or signal.get("alert_stage") or "").lower()
        if stage not in {"watch", "trade"}:
            continue
        if not _meets_min_grade(signal.get("grade"), "B"):
            continue
        return signal
    return None


def _current_native_from_candidate(candidate: dict) -> float:
    try:
        return float((candidate.get("signal_json") or {}).get("atr_data", {}).get("current_price") or 0)
    except (TypeError, ValueError):
        return 0.0


def _position_context_for_candidate(candidate: dict, open_positions: list, cfg: AdvisoryConfig) -> dict:
    symbol = str(candidate.get("data_symbol") or "").upper()
    side = str(candidate.get("side") or "").upper()
    current = _current_native_from_candidate(candidate)
    if not symbol or not side or not current:
        return {}
    max_adverse_pct = _env_float("ADVISORY_HOLDER_CONTEXT_MAX_ADVERSE_PCT", 1.0)
    for position in open_positions or []:
        if str(position.get("data_symbol") or "").upper() != symbol:
            continue
        if str(position.get("side") or "").upper() != side:
            continue
        entry = _position_entry_native(position)
        if not entry:
            continue
        direction = 1 if side == "BUY" else -1
        pnl_pct = ((current - entry) / entry * 100 * direction)
        meaningful = pnl_pct >= -max_adverse_pct
        return {
            "position_id": position.get("id"),
            "entry_native": round(entry, 4),
            "current_native": round(current, 4),
            "pnl_pct": round(pnl_pct, 4),
            "meaningful_holder_context": bool(meaningful),
            "max_adverse_pct": max_adverse_pct,
            "entry_eur": round(_display_price(entry, position.get("currency", "EUR"), position.get("fx_rate") or cfg.fx_rate), 4),
            "current_eur": round(_display_price(current, position.get("currency", "EUR"), position.get("fx_rate") or cfg.fx_rate), 4),
        }
    return {}


def _runner_context(candidate: dict, recent_live: list, now_cet: datetime,
                    open_positions: list, cfg: AdvisoryConfig) -> dict:
    """Classify extended same-day momentum separately from a fresh-entry alert."""
    if candidate.get("mode") != "live":
        return {}
    if candidate.get("alert_stage") not in WATCH_ALERT_STAGES:
        return {}
    if not _watch_had_late_chase(candidate):
        return {}
    if float(candidate.get("composite_score") or 0) < _env_float("ADVISORY_RUNNER_MIN_COMPOSITE", 0.25):
        return {}
    trend = (candidate.get("signal_json") or {}).get("trend_1h") or candidate.get("trend_1h_json") or {}
    if trend.get("aligned") is not True:
        return {}
    prior = _prior_runner_signal(recent_live, candidate, now_cet)
    if not prior:
        return {}
    position = _position_context_for_candidate(candidate, open_positions, cfg)
    prior_json = prior.get("signal_json") or {}
    return {
        "type": "runner_continuation",
        "prior_signal_id": prior.get("id"),
        "prior_created_at": prior.get("created_at"),
        "prior_grade": prior.get("grade"),
        "prior_stage": prior_json.get("alert_stage") or prior.get("alert_stage") or "watch",
        "holder_context": position if position.get("meaningful_holder_context") else {},
        "position_context": position,
    }


def _watch_repeat_blocked(recent_watch: dict, candidate: dict) -> bool:
    if not recent_watch or candidate.get("alert_stage") not in WATCH_ALERT_STAGES:
        return False
    signal_json = recent_watch.get("signal_json") or {}
    if candidate.get("runner_context") and not signal_json.get("runner_context"):
        return False
    old_stage = str(signal_json.get("alert_stage") or recent_watch.get("alert_stage") or "watch")
    new_stage = str(candidate.get("alert_stage") or "")
    if ALERT_STAGE_RANK.get(new_stage, -1) > ALERT_STAGE_RANK.get(old_stage, -1):
        return False
    old_grade = str(recent_watch.get("grade") or "").upper()
    new_grade = str(candidate.get("grade") or "").upper()
    if _grade_rank(new_grade) > _grade_rank(old_grade):
        return False
    try:
        old_composite = float(recent_watch.get("composite_score") or signal_json.get("composite_score") or 0)
        new_composite = float(candidate.get("composite_score") or 0)
    except (TypeError, ValueError):
        old_composite = new_composite = 0.0
    min_delta = _env_float("ADVISORY_WATCH_REPEAT_MIN_COMPOSITE_DELTA", 0.10)
    if new_composite - old_composite >= min_delta:
        return False
    try:
        old_breakout = float(recent_watch.get("breakout_quality") or 0)
        new_breakout = float(candidate.get("breakout_quality") or 0)
    except (TypeError, ValueError):
        old_breakout = new_breakout = 0.0
    breakout_delta = _env_float("ADVISORY_WATCH_REPEAT_MIN_BREAKOUT_DELTA", 0.15)
    if new_breakout - old_breakout >= breakout_delta:
        return False
    return True


def _alerted_symbol_in_session(recent_live: list, symbol: str, market: str,
                               now_cet: datetime) -> bool:
    window = _window_name(market, now_cet)
    if not window:
        return False
    session_start_utc = _session_start_cet(market, window, now_cet).astimezone(timezone.utc)
    symbol = symbol.upper()
    for signal in recent_live:
        if str(signal.get("market", "")).upper() != market:
            continue
        if str(signal.get("data_symbol", "")).upper() != symbol:
            continue
        if str(signal.get("status")) not in LIVE_SIGNAL_STATUSES:
            continue
        created_at = _parse_dt(signal.get("created_at"))
        if created_at and created_at >= session_start_utc:
            return True
    return False


def _is_signal_in_current_session(signal: dict, now_cet: datetime) -> bool:
    market = str(signal.get("market", "")).upper()
    window = _window_name(market, now_cet)
    if not window:
        return False
    created_at = _parse_dt(signal.get("created_at"))
    if not created_at:
        return False
    session_start_utc = _session_start_cet(market, window, now_cet).astimezone(timezone.utc)
    return created_at >= session_start_utc


def _currency_symbol(currency: str) -> str:
    return "$" if currency == "USD" else "€"


PRICE_LEVEL_KEYS = (
    "entry_min",
    "entry_max",
    "do_not_chase_price",
    "stop_price",
    "target_1",
    "target_2",
)


def _display_price(value: float, currency: str, fx_rate: float) -> float:
    value = float(value or 0)
    if currency == "USD":
        return value / max(float(fx_rate or 0), 0.0001)
    return value


def _display_levels(signal: dict) -> dict:
    currency = signal.get("currency", "EUR")
    fx_rate = float(signal.get("fx_rate") or 1.0)
    levels = {
        f"{key}_eur": round(_display_price(signal.get(key), currency, fx_rate), 4)
        for key in PRICE_LEVEL_KEYS
        if signal.get(key) is not None
    }
    levels.update({
        "display_currency": "EUR",
        "native_currency": currency,
        "fx_rate": fx_rate,
        "fx_rate_source": signal.get("fx_rate_source") or "unknown",
        "fx_rate_fetched_at": signal.get("fx_rate_fetched_at") or "",
    })
    return levels


def _eur_price(signal: dict, key: str) -> float:
    return _display_price(signal.get(key), signal.get("currency", "EUR"), signal.get("fx_rate") or 1.0)


def _native_ref_line(signal: dict) -> str:
    if signal.get("currency") != "USD":
        return ""
    source = signal.get("fx_rate_source") or "unknown"
    return (
        f"Native ref: ${signal['entry_min']:.2f}-${signal['entry_max']:.2f}; "
        f"stop ${signal['stop_price']:.2f}; T1 ${signal['target_1']:.2f}; "
        f"EUR/USD {float(signal['fx_rate']):.4f} ({source}).\n"
    )


def _signal_score(signals: dict, name: str) -> float:
    try:
        return float((signals or {}).get(name, {}).get("score", 0) or 0)
    except Exception:
        return 0.0


def _breakout_quality(side: str, composite: float, signals: dict, market_regime: str = "") -> float:
    direction = 1 if side == "BUY" else -1
    components = [
        max(0.0, min(_signal_score(signals, "macd_crossover") * direction, 1.0)),
        max(0.0, min(_signal_score(signals, "relative_strength") * direction, 1.0)),
        max(0.0, min(_signal_score(signals, "vwap_deviation") * direction, 1.0)),
        max(0.0, min(_signal_score(signals, "orb") * direction, 1.0)),
        max(0.0, min((float(composite or 0) * direction) / 0.6, 1.0)),
    ]
    if side == "BUY" and str(market_regime).lower() in {"bull", "transitioning"}:
        components.append(0.75)
    return round(sum(components) / len(components), 4)


def _grade(composite: float, breakout_quality: float, orb_active: bool) -> str:
    if composite >= 0.55 and breakout_quality >= 0.60:
        return "A+"
    if composite >= 0.45 and (breakout_quality >= 0.45 or orb_active):
        return "A"
    if composite >= 0.35:
        return "B"
    return "C"


_US_LATE_PHASE_WINDOWS = {"us_midday", "us_power_hour", "us_close"}


def _intraday_grade_cap(grade: str, side: str, signals: dict, window: str) -> tuple[str, Optional[dict]]:
    """Cap open-window grades when real-time ORB and VWAP both strongly oppose the setup.

    For us_open: cap A+ → B and A → B when ORB + VWAP both oppose.
    For later phases (us_midday, us_power_hour, us_close): only cap A+ → A (softer cap),
    and only when both ORB and VWAP strongly oppose (stricter thresholds).
    """
    direction = 1 if side == "BUY" else -1
    orb_score = _signal_score(signals, "orb")
    vwap_score = _signal_score(signals, "vwap_deviation")

    if window == "us_open" and grade in {"A", "A+"}:
        intraday_weak = (orb_score * direction < -0.5) and (vwap_score * direction < -0.3)
        if not intraday_weak:
            return grade, None
        return "B", {
            "reason": "orb_vwap_intraday_grade_cap",
            "original_grade": grade,
            "capped_grade": "B",
            "orb_score": round(orb_score, 4),
            "vwap_score": round(vwap_score, 4),
            "window": window,
        }

    if window in _US_LATE_PHASE_WINDOWS and grade == "A+":
        # Softer cap for off-peak phases: A+ → A only when ORB and VWAP strongly oppose
        late_phase_weak = (orb_score * direction < -0.65) and (vwap_score * direction < -0.45)
        if not late_phase_weak:
            return grade, None
        return "A", {
            "reason": "orb_vwap_late_phase_grade_cap",
            "original_grade": "A+",
            "capped_grade": "A",
            "orb_score": round(orb_score, 4),
            "vwap_score": round(vwap_score, 4),
            "window": window,
        }

    return grade, None


def _premium_setup_flag(side: str, signals: dict) -> dict:
    direction = 1 if side == "BUY" else -1
    macd_score = _signal_score(signals, "macd_crossover")
    rs_score = _signal_score(signals, "relative_strength")
    premium = (macd_score * direction >= 0.90) and (rs_score * direction >= 0.90)
    return {
        "premium_setup": premium,
        "macd_score": round(macd_score, 4),
        "relative_strength_score": round(rs_score, 4),
        "rule": "macd_and_relative_strength_aligned_0_90",
    }


def _grade_rank(grade: str) -> int:
    return {"C": 0, "B": 1, "A": 2, "A+": 3}.get(str(grade).upper(), -1)


def _meets_min_grade(grade: str, min_grade: str) -> bool:
    return _grade_rank(grade) >= _grade_rank(min_grade)


def _data_quality(symbol: str, market: str, listing_type: str = None, window: str = None) -> dict:
    try:
        if _get_bars is not None:
            bars = _get_bars(symbol, period="1d", interval="1m")
        else:
            import yfinance as yf
            bars = yf.download(symbol, period="1d", interval="1m", progress=False, auto_adjust=True)
        if bars is None or bars.empty:
            return {"ok": False, "reason": "missing_1m_bars"}
        rows = len(bars)
        latest = bars.index[-1]
        try:
            latest_utc = latest.to_pydatetime()
            if latest_utc.tzinfo is None:
                latest_utc = latest_utc.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - latest_utc.astimezone(timezone.utc)).total_seconds() / 60
        except Exception:
            age_min = 0
        recent = bars.tail(20)
        avg_volume = float(recent["Volume"].mean()) if "Volume" in recent else 0.0
        if listing_type == "eu_us_mirror":
            min_rows = 20
        elif market == "EU":
            min_rows = 45
        else:
            min_rows = 30
        early_us_min_rows = _env_int("ADVISORY_US_EARLY_MIN_ROWS", 10)
        if rows < min_rows:
            # TR morning watch: accept thin pre-Xetra bars from L&S Exchange
            if listing_type == "eu_us_mirror" and window == "tr_morning_watch" and rows >= 3 and age_min <= 30:
                close = float(bars["Close"].squeeze().iloc[-1])
                return {
                    "ok": True,
                    "rows": rows,
                    "age_minutes": round(age_min, 1),
                    "avg_recent_volume": round(avg_volume, 2),
                    "last_price": round(close, 4),
                    "tr_early_relaxed": True,
                    "required_rows": min_rows,
                }
            if market == "US" and rows >= early_us_min_rows and age_min <= 5:
                close = float(bars["Close"].squeeze().iloc[-1])
                return {
                    "ok": True,
                    "rows": rows,
                    "age_minutes": round(age_min, 1),
                    "avg_recent_volume": round(avg_volume, 2),
                    "last_price": round(close, 4),
                    "early_session_relaxed": True,
                    "required_rows": min_rows,
                }
            return {"ok": False, "reason": "too_few_bars", "rows": rows}
        if age_min > 20:
            return {"ok": False, "reason": "stale_bars", "age_minutes": round(age_min, 1), "rows": rows}
        if avg_volume <= 0:
            return {"ok": False, "reason": "zero_recent_volume", "rows": rows}
        if listing_type == "eu_us_mirror" and avg_volume < 300 and window != "tr_morning_watch":
            return {
                "ok": False,
                "reason": "eu_mirror_thin_volume",
                "avg_recent_volume": round(avg_volume, 2),
                "rows": rows,
            }
        close = float(bars["Close"].squeeze().iloc[-1])
        return {
            "ok": True,
            "rows": rows,
            "age_minutes": round(age_min, 1),
            "avg_recent_volume": round(avg_volume, 2),
            "last_price": round(close, 4),
        }
    except Exception as e:
        return {"ok": False, "reason": "data_quality_error", "error": str(e)[:120]}


def _bar_column(bars, name: str):
    column = bars[name]
    return column.squeeze() if hasattr(column, "squeeze") else column


def _ignition_check(symbol: str, side: str, composite: float, atr_data: dict) -> dict:
    debug = _env_bool("ADVISORY_IGNITION_DEBUG", False)

    def _diag(reason: str, **detail):
        if not debug:
            return
        log_event("INFO", "advisory_ignition_debug", {
            "symbol": symbol,
            "side": side,
            "composite": round(float(composite or 0), 4),
            "reason": reason,
            **detail,
        })

    min_composite = _env_float("ADVISORY_IGNITION_MIN_COMPOSITE", 0.05)
    if abs(float(composite or 0)) < min_composite:
        _diag("below_min_composite", min_composite=min_composite)
        return {}
    try:
        if _get_bars is not None:
            bars = _get_bars(symbol, period="1d", interval="1m")
        else:
            import yfinance as yf
            bars = yf.download(symbol, period="1d", interval="1m", progress=False, auto_adjust=True)
        bar_count = 0 if bars is None or getattr(bars, "empty", True) else len(bars)
        if bars is None or bars.empty or len(bars) < 6:
            _diag("insufficient_bars", bar_count=bar_count)
            return {}
        close = _bar_column(bars, "Close")
        volume = _bar_column(bars, "Volume")
        window_bars = min(5, len(bars) - 1)
        previous_close = float(close.iloc[-1 - window_bars])
        latest_close = float(close.iloc[-1])
        if previous_close <= 0:
            _diag("bad_previous_close", bar_count=bar_count)
            return {}
        move_pct = (latest_close - previous_close) / previous_close * 100
        if side == "BUY" and move_pct <= 0:
            _diag("wrong_direction_buy", bar_count=bar_count, move_pct=round(move_pct, 3))
            return {}
        if side == "SELL" and move_pct >= 0:
            _diag("wrong_direction_sell", bar_count=bar_count, move_pct=round(move_pct, 3))
            return {}
        atr_pct = float((atr_data or {}).get("atr_pct") or 0)
        if atr_pct <= 0:
            _diag("no_atr", bar_count=bar_count, move_pct=round(move_pct, 3))
            return {}
        atr_multiple = abs(move_pct) / atr_pct
        recent_volume = float(volume.iloc[-window_bars:].mean())
        prior_volume = volume.iloc[:-window_bars].tail(20)
        prior_avg_volume = float(prior_volume.mean()) if len(prior_volume) else 0.0
        if prior_avg_volume <= 0:
            _diag("no_prior_volume", bar_count=bar_count, atr_multiple=round(atr_multiple, 2))
            return {}
        volume_ratio = recent_volume / prior_avg_volume
        min_volume_ratio = _env_float("ADVISORY_IGNITION_MIN_VOLUME_RATIO", 2.0)
        min_atr_move = _env_float("ADVISORY_IGNITION_MIN_ATR_MOVE", 0.50)
        if volume_ratio < min_volume_ratio or atr_multiple < min_atr_move:
            _diag("below_thresholds", bar_count=bar_count,
                  volume_ratio=round(volume_ratio, 2),
                  atr_multiple=round(atr_multiple, 2),
                  min_volume_ratio=min_volume_ratio,
                  min_atr_move=min_atr_move)
            return {}
        result = {
            "reason": "momentum_ignition",
            "window_bars": window_bars,
            "volume_ratio": round(volume_ratio, 2),
            "atr_multiple": round(atr_multiple, 2),
            "move_pct": round(move_pct, 3),
            "min_volume_ratio": min_volume_ratio,
            "min_atr_move": min_atr_move,
        }
        _diag("fired", bar_count=bar_count, **{k: result[k] for k in
              ("volume_ratio", "atr_multiple", "move_pct")})
        return result
    except Exception as e:
        _diag("exception", error=str(e)[:120])
        return {}


def _entry_plan(price: float, side: str, atr_pct: float, currency: str, cfg: AdvisoryConfig,
                grade: str) -> dict:
    atr_pct = max(float(atr_pct or 1.2), 0.5)
    stop_pct = min(max(atr_pct * 1.4, 0.8), 3.2)
    target_1_pct = stop_pct * 1.8
    target_2_pct = stop_pct * 3.0
    chase_pct = min(0.35, max(0.12, atr_pct * 0.20))
    entry_band_pct = min(0.22, max(0.08, atr_pct * 0.12))
    if side == "BUY":
        entry_min = price * (1 - entry_band_pct / 100)
        entry_max = price * (1 + entry_band_pct / 100)
        do_not_chase = price * (1 + chase_pct / 100)
        stop = price * (1 - stop_pct / 100)
        target_1 = price * (1 + target_1_pct / 100)
        target_2 = price * (1 + target_2_pct / 100)
    else:
        entry_min = price * (1 - entry_band_pct / 100)
        entry_max = price * (1 + entry_band_pct / 100)
        do_not_chase = price * (1 - chase_pct / 100)
        stop = price * (1 + stop_pct / 100)
        target_1 = price * (1 - target_1_pct / 100)
        target_2 = price * (1 - target_2_pct / 100)
    max_size = cfg.a_plus_max_size_eur if grade == "A+" else cfg.default_size_eur
    risk_fraction = stop_pct / 100
    risk_sized = cfg.risk_per_trade_eur / risk_fraction if risk_fraction > 0 else cfg.default_size_eur
    size_eur = min(max_size, risk_sized, cfg.capital_eur * 0.30)
    risk_eur = size_eur * risk_fraction
    return {
        "entry_min": round(entry_min, 4),
        "entry_max": round(entry_max, 4),
        "do_not_chase_price": round(do_not_chase, 4),
        "stop_price": round(stop, 4),
        "target_1": round(target_1, 4),
        "target_2": round(target_2, 4),
        "suggested_size_eur": round(size_eur, 2),
        "risk_eur": round(risk_eur, 2),
        "risk_pct": round(stop_pct, 4),
        "reward_risk": 1.8,
        "currency_symbol": _currency_symbol(currency),
    }


def _send_discord(text: str, webhook_url: str) -> bool:
    if not webhook_url:
        return False
    try:
        import requests
        return requests.post(webhook_url, json={"content": text}, timeout=10).ok
    except Exception:
        return False


def _ordered_markets(cfg: AdvisoryConfig) -> list[str]:
    live = [market for market in ["US", "EU"] if market in cfg.markets and market in cfg.live_markets]
    shadow = [
        market for market in ["US", "EU"]
        if market in cfg.markets and market in cfg.shadow_markets and market not in live
    ]
    other = sorted(cfg.markets - set(live) - set(shadow))
    return live + shadow + other


def _compact_signal_payload(signal_result: dict) -> dict:
    signals = signal_result.get("signals") or {}
    return {
        "composite_score": signal_result.get("composite_score"),
        "scores": {
            name: payload.get("score")
            for name, payload in signals.items()
            if isinstance(payload, dict)
        },
        "atr_data": signal_result.get("atr_data") or {},
        "block_reasons": signal_result.get("block_reasons") or [],
    }


def _trend_1h_alignment(symbol: str, side: str, composite: float) -> dict:
    """Log-only 1h trend context for later replay analysis; never gates alerts."""
    try:
        from backend.signals.engine import _get_bars
    except Exception:
        return {"status": "unavailable", "reason": "bar_fetch_unavailable"}

    try:
        bars = _get_bars(symbol, period="5d", interval="1h")
    except Exception as exc:
        return {"status": "unavailable", "reason": str(exc)[:80]}
    if bars is None or getattr(bars, "empty", True):
        return {"status": "unavailable", "reason": "no_bars"}

    try:
        close = bars["Close"].dropna()
        if len(close) < 8:
            return {"status": "insufficient_data", "bars": int(len(close))}
        last = float(close.iloc[-1])
        first_6h = float(close.iloc[-7])
        first_3h = float(close.iloc[-4])
        ema_fast = close.ewm(span=3, adjust=False).mean()
        ema_slow = close.ewm(span=8, adjust=False).mean()
        ema_spread_pct = (
            (float(ema_fast.iloc[-1]) - float(ema_slow.iloc[-1])) / last * 100
            if last else 0.0
        )
        ret_6h_pct = ((last - first_6h) / first_6h * 100) if first_6h else 0.0
        ret_3h_pct = ((last - first_3h) / first_3h * 100) if first_3h else 0.0
        trend_score = max(-1.0, min(1.0, (ret_6h_pct / 2.0) + (ema_spread_pct / 0.75)))
        trend_direction = "bullish" if trend_score > 0.15 else ("bearish" if trend_score < -0.15 else "neutral")
        side = str(side or "BUY").upper()
        aligned = (
            trend_direction == "bullish" if side == "BUY"
            else trend_direction == "bearish" if side == "SELL"
            else False
        )
        return {
            "status": "ok",
            "aligned": bool(aligned),
            "direction": trend_direction,
            "score": round(trend_score, 4),
            "ret_3h_pct": round(ret_3h_pct, 4),
            "ret_6h_pct": round(ret_6h_pct, 4),
            "ema_spread_pct": round(ema_spread_pct, 4),
            "bars": int(len(close)),
            "side": side,
            "composite_score": round(float(composite or 0.0), 4),
        }
    except Exception as exc:
        return {"status": "unavailable", "reason": str(exc)[:80]}


def _format_trade_card(signal: dict) -> str:
    # Special formatting for downside-risk informational alerts
    if signal.get("downside_risk"):
        sym = signal["data_symbol"]
        composite = float(signal.get("composite_score") or 0)
        sig_json = signal.get("signal_json") or {}
        sigs = sig_json.get("signals") or {}
        vwap = (sigs.get("vwap_deviation") or {}).get("score", 0)
        macd = (sigs.get("macd_crossover") or {}).get("score", 0)
        rsi = (sigs.get("rsi_divergence") or {}).get("score", 0)
        return (
            f"**DOWNSIDE RISK - {sym} - {signal.get('grade', '?')} - {signal.get('market', '?')}**\n"
            f"Composite: {composite:+.3f} | VWAP: {vwap:+.2f} | MACD: {macd:+.2f} | RSI: {rsi:+.2f}\n"
            f"No position recommended — monitor for stop-zone or exit if holding."
        )

    sym = signal["data_symbol"]
    side = str(signal.get("side") or "BUY").upper()
    is_shadow = signal.get("mode") != "live"
    is_watch = signal.get("alert_stage") in WATCH_ALERT_STAGES
    is_ignition = signal.get("alert_stage") == "ignition"
    is_late_chase = bool(signal.get("late_chase_json"))
    is_runner = bool(signal.get("runner_context"))
    is_holding = bool(signal.get("holding_context"))
    notional = f"€{signal['suggested_size_eur']:.0f}"
    range_action = (
        "sell/short only inside range; avoid chasing below max extension."
        if side == "SELL"
        else "buy only inside range; avoid chasing above max."
    )
    entry_min_eur = _eur_price(signal, "entry_min")
    entry_max_eur = _eur_price(signal, "entry_max")
    chase_eur = _eur_price(signal, "do_not_chase_price")
    stop_eur = _eur_price(signal, "stop_price")
    target_1_eur = _eur_price(signal, "target_1")
    target_2_eur = _eur_price(signal, "target_2")
    quick_why = signal["rationale"].split(", window ")[0]

    if is_holding and is_runner:
        prefix = "RUNNER HOLD"
    elif is_holding:
        prefix = "HOLD"
    elif is_runner:
        prefix = "RUNNER WATCH"
    elif is_ignition:
        prefix = "MOMENTUM IGNITION"
    elif is_watch:
        prefix = "WATCH ONLY"
    elif is_shadow:
        prefix = "SHADOW OBSERVATION"
    else:
        prefix = "LIVE TRADE ALERT"

    qualifier = ""
    if is_late_chase:
        qualifier = " - extended"
    elif signal.get("pullback_confirmed"):
        qualifier = " - pullback confirmed"
    elif is_holding:
        qualifier = " - runner check"

    first_line = f"**{prefix} - {side} {sym} - {signal['grade']} - {signal['market']}{qualifier}**"

    notes = []
    if is_shadow:
        if str(signal.get("grade", "")).upper() == "C":
            notes.append(f"LOW-GRADE SHADOW {side} OPPORTUNITY: log only.")
        notes.append("Action: log only; do not trade from shadow mode.")
    elif is_holding and is_runner:
        holder = signal.get("holding_context") or {}
        notes.append(
            "Why: same-day runner is still aligned; prior B+ advisory confirmed the move "
            f"and open position is {holder.get('pnl_pct', 0):+.2f}% from recorded entry."
        )
        notes.append("Action: holder context; consider holding/trailing toward T1/T2, not a fresh chase.")
    elif is_holding:
        notes.append("Action: holder context; manage the open trade, not a fresh chase.")
    elif is_ignition:
        detail = signal.get("ignition_json") or {}
        notes.append(
            "Why: momentum ignition "
            f"({detail.get('move_pct')}% over {detail.get('window_bars')}m, "
            f"{detail.get('atr_multiple')}x ATR, {detail.get('volume_ratio')}x volume)."
        )
        notes.append("Action: watch for follow-through; use the band as tentative only.")
    elif is_runner:
        detail = signal.get("late_chase_json") or {}
        notes.append(
            "Why: same-day runner continuation; prior B+ advisory confirmed the move, "
            f"but VWAP deviation is {detail.get('pct_deviation')}% vs {detail.get('threshold_pct')}%."
        )
        notes.append("Action: active-trader/holder context; fresh entry only on pullback.")
    elif is_watch:
        if is_ignition:
            pass
        elif is_late_chase:
            detail = signal.get("late_chase_json") or {}
            notes.append(
                "Why: setup is strong but extended "
                f"(VWAP deviation {detail.get('pct_deviation')}% vs {detail.get('threshold_pct')}%)."
            )
            notes.append("Action: wait for pullback into the band; no fresh chase.")
        else:
            notes.append("Early signal only.")
            notes.append(f"Why: {quick_why}.")
            notes.append("Action: prepare the chart; buy only if the setup confirms in range.")
    elif signal.get("pullback_confirmed"):
        notes.append("Why: prior late-chase extension cleared; entry band is valid again.")
        notes.append(f"Action: {range_action}")
    else:
        notes.append(f"Why: {quick_why}.")
        notes.append(f"Action: {range_action}")
        try:
            notes.append(f"EV: {float(signal.get('ev_net_pct') or 0):+.2f}%")
        except (TypeError, ValueError):
            pass

    if is_watch:
        if is_ignition:
            entry_line = f"Watch band: €{entry_min_eur:.2f}-€{entry_max_eur:.2f} | Valid: {signal['valid_until_cet']}"
        else:
            entry_line = f"Pullback zone: €{entry_min_eur:.2f}-€{entry_max_eur:.2f} | Valid: {signal['valid_until_cet']}"
    else:
        entry_line = (
            f"LIMIT {side}: €{entry_min_eur:.2f}-€{entry_max_eur:.2f} | "
            f"Max: €{chase_eur:.2f} | Valid: {signal['valid_until_cet']}"
        )
    levels_line = (
        f"Levels: stop €{stop_eur:.2f} | T1 €{target_1_eur:.2f} | "
        f"T2 €{target_2_eur:.2f} | Exit by {signal['time_exit_cet']}"
    )
    size_label = "Tentative size" if is_watch else "Size"
    size_line = f"{size_label}: {notional} | Risk: ~€{signal['risk_eur']:.0f}"
    if signal.get("listing_type") == "eu_us_mirror":
        notes.append(f"Pre-Nasdaq mirror of {signal.get('primary_symbol')}: early EU read; execute on primary listing.")
    if signal.get("liquidity_note"):
        notes.append(f"Liquidity: {signal['liquidity_note']}")
    if (
        signal.get("mode") == "live"
        and signal.get("alert_stage") == "trade"
        and signal.get("id")
        and not is_shadow
    ):
        base_url = _env_value("ADVISORY_DASHBOARD_URL", "https://tradesigns.streamlit.app").rstrip("/")
        notes.append(f"Mark as taken: {base_url}/?mark_id={signal['id']}")
    return (
        f"{first_line}\n"
        f"{entry_line}\n"
        f"{levels_line}\n"
        f"{size_line}\n\n"
        + "\n".join(notes)
    )


def _market_context(market: str) -> dict:
    benchmarks = {"US": ["SPY", "QQQ"], "EU": ["^STOXX50E", "^GDAXI"]}
    context = {"market": market, "benchmarks": {}}
    try:
        import yfinance as yf
        for symbol in benchmarks.get(market, []):
            bars = yf.download(symbol, period="1d", interval="5m", progress=False, auto_adjust=True)
            if bars is None or bars.empty:
                continue
            first = float(bars["Close"].squeeze().iloc[0])
            last = float(bars["Close"].squeeze().iloc[-1])
            context["benchmarks"][symbol] = round((last - first) / first * 100, 3) if first else 0
    except Exception as e:
        context["error"] = str(e)[:120]
    return context


def _weights_for_market(market: str, listing_type: str = None) -> dict:
    if listing_type == "eu_us_mirror":
        return EU_MIRROR_WEIGHTS
    if market == "EU":
        return EU_WEIGHTS
    return get_profile(_env_value("RISK_PROFILE", "moderate")).get("signal_weights", {})


def _should_send_discord(candidate: dict, cfg: AdvisoryConfig) -> bool:
    # Downside-risk alerts always send if live — bypass grade gate
    if candidate.get("downside_risk"):
        return candidate.get("mode") == "live"
    # benchmark_only tickers (SPY, QQQ) are logged for context but never get a Discord card
    if candidate.get("benchmark_only") or not candidate.get("trade_target", True):
        return False
    if candidate.get("alert_stage") in WATCH_ALERT_STAGES:
        return candidate.get("mode") == "live"
    if candidate.get("mode") == "live":
        if not _meets_min_grade(candidate.get("grade"), cfg.min_discord_grade):
            return False
        return True
    if not _meets_min_grade(candidate.get("grade"), cfg.shadow_min_discord_grade):
        return False
    return str(candidate.get("market", "")).upper() in cfg.shadow_discord_markets


def _latest_native_price(symbol: str) -> Optional[float]:
    try:
        if _get_bars is None:
            return None
        bars = _get_bars(symbol, period="1d", interval="1m")
        if bars is None or bars.empty:
            return None
        close = bars["Close"].squeeze()
        return float(close.dropna().iloc[-1])
    except Exception:
        return None


def _monitor_alerts(position: dict) -> set[str]:
    monitor = position.get("exit_monitor_json") or {}
    if not isinstance(monitor, dict):
        return set()
    return {str(item) for item in (monitor.get("alerts") or [])}


def _with_monitor_alert(position: dict, alert_type: str, payload: dict) -> dict:
    monitor = position.get("exit_monitor_json") or {}
    if not isinstance(monitor, dict):
        monitor = {}
    new_alerts = ["t1", "t2"] if alert_type == "t2" else [alert_type]
    alerts = list(dict.fromkeys([*(monitor.get("alerts") or []), *new_alerts]))
    monitor.update({
        "alerts": alerts,
        "last_alert": alert_type,
        "last_checked_at": datetime.utcnow().isoformat() + "Z",
        **payload,
    })
    return monitor


def _monitor_checked_recently(position: dict, now_utc: datetime, minutes: int = 10) -> bool:
    monitor = position.get("exit_monitor_json") or {}
    if not isinstance(monitor, dict):
        return False
    last_checked = _parse_dt(monitor.get("last_checked_at"))
    if not last_checked:
        return False
    return (now_utc - last_checked).total_seconds() < minutes * 60


def _position_entry_native(position: dict) -> float:
    try:
        manual = float(position.get("manual_entry_price") or 0)
        if manual > 0:
            return manual
    except (TypeError, ValueError):
        pass
    try:
        entry_min = float(position.get("entry_min") or 0)
        entry_max = float(position.get("entry_max") or 0)
    except (TypeError, ValueError):
        return 0.0
    if entry_min and entry_max:
        return (entry_min + entry_max) / 2.0
    return entry_min or entry_max


def _format_exit_alert(position: dict, alert_type: str, current_native: float, cfg: AdvisoryConfig) -> str:
    sym = position.get("data_symbol")
    side = str(position.get("side") or "BUY").upper()
    entry_native = _position_entry_native(position)
    current_eur = _display_price(current_native, position.get("currency", "EUR"), position.get("fx_rate") or cfg.fx_rate)
    entry_eur = _display_price(entry_native, position.get("currency", "EUR"), position.get("fx_rate") or cfg.fx_rate)
    stop_eur = _display_price(position.get("stop_price"), position.get("currency", "EUR"), position.get("fx_rate") or cfg.fx_rate)
    t1_eur = _display_price(position.get("target_1"), position.get("currency", "EUR"), position.get("fx_rate") or cfg.fx_rate)
    t2_eur = _display_price(position.get("target_2"), position.get("currency", "EUR"), position.get("fx_rate") or cfg.fx_rate)
    direction = 1 if side == "BUY" else -1
    pnl_pct = ((current_native - entry_native) / entry_native * 100 * direction) if entry_native else 0.0
    size_eur = float((position.get("exit_monitor_json") or {}).get("size_eur") or position.get("suggested_size_eur") or 0)
    pnl_eur = size_eur * pnl_pct / 100.0

    titles = {
        "t1": "T1 HIT",
        "t2": "T2 HIT",
        "stop": "STOP ZONE",
        "time": "TIME WINDOW CLOSING",
    }
    title = titles.get(alert_type, "POSITION UPDATE")
    if alert_type == "t1":
        action = f"Action: consider trimming part and moving stop near breakeven €{entry_eur:.2f}."
    elif alert_type == "t2":
        action = "Action: consider exiting more, or trail tightly if momentum is still strong."
    elif alert_type == "stop":
        action = "Action: protect capital; this is the planned invalidation zone."
    else:
        action = "Action: reassess manually; advisory validity window is nearly over."
    return (
        f"**{title} - {sym} {side} - {position.get('grade')}**\n"
        f"Current: €{current_eur:.2f} | Entry: €{entry_eur:.2f} | P&L: {pnl_pct:+.2f}% (~€{pnl_eur:+.0f})\n"
        f"Levels: stop €{stop_eur:.2f} | T1 €{t1_eur:.2f} | T2 €{t2_eur:.2f}\n"
        f"{action}"
    )


def _monitor_open_positions(cfg: AdvisoryConfig, now_cet: datetime) -> list[dict]:
    """Send one-shot recommendation alerts for manually entered advisory rows."""
    if not cfg.discord_webhook_url:
        return []
    emitted = []
    now_utc = now_cet.astimezone(timezone.utc)
    for position in get_open_advisory_positions(max_age_days=7):
        symbol = position.get("data_symbol")
        if not symbol:
            continue
        current = _latest_native_price(symbol)
        if not current:
            continue
        side = str(position.get("side") or "BUY").upper()
        direction = 1 if side == "BUY" else -1
        alerts = _monitor_alerts(position)
        alert_type = None
        try:
            stop = float(position.get("stop_price") or 0)
            t1 = float(position.get("target_1") or 0)
            t2 = float(position.get("target_2") or 0)
        except (TypeError, ValueError):
            stop = t1 = t2 = 0.0
        if stop and ((side == "BUY" and current <= stop) or (side == "SELL" and current >= stop)):
            if "stop" not in alerts:
                alert_type = "stop"
        elif t2 and ((side == "BUY" and current >= t2) or (side == "SELL" and current <= t2)):
            if "t2" not in alerts:
                alert_type = "t2"
        elif t1 and ((side == "BUY" and current >= t1) or (side == "SELL" and current <= t1)):
            if "t1" not in alerts:
                alert_type = "t1"
        if not alert_type:
            valid_until = _parse_dt(position.get("valid_until"))
            if valid_until and 0 <= (valid_until - now_utc).total_seconds() <= 15 * 60 and "time" not in alerts:
                alert_type = "time"
        if not alert_type:
            if _monitor_checked_recently(position, now_utc):
                continue
            update_advisory_exit_status(position["id"], {
                "exit_monitor_json": _with_monitor_alert(position, "checked", {
                    "last_price_native": round(current, 4),
                }),
            })
            continue
        message = _format_exit_alert(position, alert_type, current, cfg)
        _send_discord(message, cfg.discord_webhook_url)
        monitor = _with_monitor_alert(position, alert_type, {
            "last_price_native": round(current, 4),
            "last_price_eur": round(_display_price(current, position.get("currency", "EUR"), position.get("fx_rate") or cfg.fx_rate), 4),
        })
        updates = {
            "exit_alert_type": alert_type,
            "exit_alerted_at": datetime.utcnow().isoformat() + "Z",
            "exit_monitor_json": monitor,
        }
        if alert_type == "t1":
            updates["t1_alerted"] = True
        update_advisory_exit_status(position["id"], updates)
        emitted.append({"symbol": symbol, "alert_type": alert_type})
    return emitted


def _monitor_virtual_positions(cfg: "AdvisoryConfig", now_cet: datetime) -> list:
    """Monitor open virtual positions for exit level hits and send alerts."""
    if not cfg.discord_webhook_url:
        return []
    emitted = []
    for position in get_open_virtual_positions(max_age_days=3):
        symbol = position.get("data_symbol")
        if not symbol:
            continue
        current = _latest_native_price(symbol)
        if not current:
            continue
        side = str(position.get("side") or "BUY").upper()
        entry = float(position.get("entry_price_native") or 0)
        if not entry:
            continue
        direction = 1 if side == "BUY" else -1
        pnl_pct = (current - entry) / entry * 100 * direction
        stop = float(position.get("stop_price") or 0)
        t1 = float(position.get("target_1") or 0)
        t2 = float(position.get("target_2") or 0)
        grade = position.get("grade") or "?"
        currency = position.get("currency") or "USD"
        fx = float(position.get("fx_rate") or cfg.fx_rate or 1.0)
        monitor_json = position.get("exit_monitor_json") or {}
        if not isinstance(monitor_json, dict):
            monitor_json = {}
        alerts_sent = set(monitor_json.get("alerts") or [])

        hit_level = None
        new_status = None
        if stop and direction * (current - stop) <= 0 and "stop" not in alerts_sent:
            hit_level = "stop"
            new_status = "hit_stop"
        elif t2 and direction * (current - t2) >= 0 and "t2" not in alerts_sent:
            hit_level = "t2"
            new_status = "hit_t2"
        elif t1 and direction * (current - t1) >= 0 and "t1" not in alerts_sent:
            hit_level = "t1"
            new_status = "hit_t1"

        if not hit_level:
            continue

        current_eur = _display_price(current, currency, fx)
        entry_eur = _display_price(entry, currency, fx)
        message = (
            f"**VIRTUAL EXIT - {grade} - {symbol} {side}**\n"
            f"Level: {hit_level.upper()} hit | Current: €{current_eur:.2f} | "
            f"Entry: €{entry_eur:.2f} | P&L: {pnl_pct:+.2f}%\n"
            f"Virtual position (auto-assumed entry on {grade} alert) — no real position tracked."
        )
        _send_discord(message, cfg.discord_webhook_url)
        alerts_sent.add(hit_level)
        monitor_json["alerts"] = list(alerts_sent)
        monitor_json["last_alert"] = hit_level
        monitor_json["last_checked_at"] = datetime.utcnow().isoformat() + "Z"

        updates = {
            "exit_monitor_json": monitor_json,
            "pnl_pct": round(pnl_pct, 4),
            "close_price_native": round(current, 4),
        }
        if new_status in {"hit_stop", "hit_t2"}:
            updates["status"] = new_status
            updates["closed_at"] = datetime.utcnow().isoformat() + "Z"
        elif new_status == "hit_t1":
            updates["status"] = "hit_t1"
            # Don't close on T1 — let it run to T2 or stop
        try:
            update_virtual_position(position["id"], updates)
        except Exception:
            pass
        emitted.append({"symbol": symbol, "alert_type": hit_level, "virtual": True})
    return emitted


def _eu_catalyst_score(item: dict, signals: dict) -> float:
    scores = [abs(_signal_score(signals, "news_sentiment"))]
    aliases = [
        item.get("data_symbol", "").split(".")[0],
        item.get("broker_display_name", ""),
    ]
    for alias in aliases:
        alias = str(alias).strip()
        if not alias:
            continue
        try:
            score, _meta = news_sentiment_score(alias)
            scores.append(abs(float(score or 0)))
        except Exception:
            continue
    return max(scores) if scores else 0.0


def _build_downside_candidate(
    symbol: str, item: dict, market: str, mode: str,
    composite: float, signal_result: dict, regime_state, window: str,
    cfg: "AdvisoryConfig",
) -> Optional[dict]:
    """Build an informational downside-risk candidate when composite is strongly negative."""
    signals = signal_result.get("signals") or {}
    side = "SELL"
    breakout = _breakout_quality(side, abs(composite), signals, getattr(regime_state, "market_regime", ""))
    orb_active = bool((signals.get("orb") or {}).get("meta", {}).get("active"))
    grade = _grade(abs(composite), breakout, orb_active)
    listing_type = item.get("listing_type")
    primary_symbol = item.get("primary_symbol")
    return {
        "data_symbol": symbol,
        "broker_display_name": item.get("broker_display_name"),
        "exchange": item.get("exchange"),
        "currency": item.get("currency", "USD" if market == "US" else "EUR"),
        "market": market,
        "mode": mode,
        "window": window,
        "side": side,
        "composite_score": round(composite, 4),
        "grade": grade,
        "alert_stage": "watch",
        "status": "sent",
        "downside_risk": True,
        "rationale": (
            f"Bearish pressure: composite={composite:.3f} | "
            f"VWAP {signals.get('vwap_deviation', {}).get('score', 0):+.2f} | "
            f"MACD {signals.get('macd_crossover', {}).get('score', 0):+.2f} | "
            f"RSI {signals.get('rsi_divergence', {}).get('score', 0):+.2f}"
        ),
        "suggested_size_eur": 0,
        "listing_type": listing_type,
        "primary_symbol": primary_symbol,
        "benchmark_only": item.get("benchmark_only", False),
        "trade_target": item.get("trade_target", True),
        "priority": item.get("priority", "medium"),
        "breakout_quality": breakout,
        "signal_json": _compact_signal_payload(signal_result),
    }


def _scan_candidate(item: dict, market: str, mode: str, cfg: AdvisoryConfig,
                    recent_trades: list, now_cet: datetime) -> Optional[dict]:
    symbol = item["data_symbol"]
    window = _window_name(market, now_cet)
    if not window:
        return None
    mirror_only_windows = item.get("mirror_only_windows")
    if mirror_only_windows and window not in mirror_only_windows:
        return None
    listing_type = item.get("listing_type")
    primary_symbol = item.get("primary_symbol")
    quality = _data_quality(symbol, market, listing_type=listing_type, window=window)
    if not quality.get("ok"):
        return {
            "market": market, "mode": mode, "status": "blocked_data_quality",
            "data_symbol": symbol, "broker_display_name": item.get("broker_display_name"),
            "exchange": item.get("exchange"), "currency": item.get("currency"),
            "listing_type": listing_type, "primary_symbol": primary_symbol,
            "origin_market": item.get("origin_market"),
            "side": "BUY", "data_quality_json": quality,
            "rationale": f"Data quality blocked: {quality.get('reason')}",
        }
    if item.get("currency") == "USD" and float(cfg.fx_rate or 0) <= 0:
        return {
            "market": market, "mode": mode, "status": "blocked_filter",
            "data_symbol": symbol, "broker_display_name": item.get("broker_display_name"),
            "exchange": item.get("exchange"), "currency": item.get("currency"),
            "listing_type": listing_type, "primary_symbol": primary_symbol,
            "origin_market": item.get("origin_market"),
            "side": "BUY",
            "rationale": "FX rate unavailable for EUR advisory display",
            "data_quality_json": quality,
            "signal_json": {"fx_rate_source": cfg.fx_rate_source},
        }

    regime_state = detect_regime(symbol)
    weights = _weights_for_market(market, listing_type=listing_type)
    signal_result = compute_all_signals(symbol, weights, regime_state=regime_state)
    if listing_type == "eu_us_mirror" and primary_symbol:
        try:
            primary_news_score, _meta = news_sentiment_score(primary_symbol)
            if signal_result.get("signals", {}).get("news_sentiment") is not None:
                signal_result["signals"]["news_sentiment"]["score"] = float(primary_news_score or 0)
                signal_result["composite_score"] = sum(
                    _signal_score(signal_result.get("signals") or {}, name) * weight
                    for name, weight in weights.items()
                )
        except Exception:
            pass
    composite = float(signal_result.get("composite_score") or 0)
    is_live_market = market in cfg.live_markets

    if composite <= 0:
        min_downside = _env_float("ADVISORY_MIN_DOWNSIDE_COMPOSITE", 0.35)
        if abs(composite) >= min_downside and is_live_market:
            return _build_downside_candidate(
                symbol=symbol, item=item, market=market, mode=mode,
                composite=composite, signal_result=signal_result,
                regime_state=regime_state, window=window, cfg=cfg,
            )
        return None

    side = "BUY" if composite > 0 else "SELL"
    if side == "SELL" and not cfg.allow_short:
        return None

    signals = signal_result.get("signals") or {}
    breakout = _breakout_quality(side, composite, signals, getattr(regime_state, "market_regime", ""))
    orb_active = bool((signals.get("orb") or {}).get("meta", {}).get("active"))
    grade = _grade(composite, breakout, orb_active)
    # TR morning watch: cap alert_stage to "watch" — no trade alerts during pre-Xetra window
    _tr_morning_watch = (window == "tr_morning_watch")
    grade_cap = None
    if is_live_market:
        grade, grade_cap = _intraday_grade_cap(grade, side, signals, window)
    premium_setup = _premium_setup_flag(side, signals)
    atr_data = signal_result.get("atr_data") or {}
    trend_1h = _trend_1h_alignment(symbol, side, composite) if is_live_market else {
        "status": "not_scored",
        "reason": "shadow_market",
    }
    late_chase = _late_chase_block(
        side,
        signals,
        atr_data,
        {
            "late_chase_block_enabled": True,
            "late_chase_atr_mult": _env_float("ADVISORY_LATE_CHASE_ATR_MULT", 1.5),
        },
    )
    ignition = _ignition_check(symbol, side, composite, atr_data) if is_live_market else {}

    trade_ready = (
        not is_live_market
        or (
            composite >= cfg.min_composite
            and grade in {"A+", "A"}
            and breakout >= cfg.min_breakout_quality
        )
    )
    watch_ready = (
        is_live_market
        and (grade != "C" or bool(late_chase))
        and composite >= cfg.min_watch_composite
        and (breakout >= cfg.min_watch_breakout_quality or orb_active)
    )
    ignition_ready = bool(ignition) and not trade_ready
    if is_live_market and not (trade_ready or watch_ready or ignition_ready):
        return None

    if trade_ready and not late_chase:
        alert_stage = "trade"
    elif ignition_ready and not watch_ready:
        alert_stage = "ignition"
    else:
        alert_stage = "watch"
    if window == "us_premarket":
        alert_stage = "ignition" if ignition_ready and not watch_ready else "watch"
    # TR morning watch: always cap to "watch" — pre-Xetra, informational only
    if _tr_morning_watch:
        alert_stage = "watch"
    plan = _entry_plan(
        quality["last_price"], side, atr_data.get("atr_pct"),
        item.get("currency", "EUR"), cfg, grade,
    )
    setup_context = {
        "breakout_quality": breakout,
        "strategy_family": "advisory_manual",
        "market": market,
    }
    ev = compute_expected_value(
        composite, plan["suggested_size_eur"], recent_trades,
        getattr(regime_state, "intraday_regime", "ranging"),
        setup_context=setup_context,
        profile={"ev_breakout_probe_min_quality": 0.65},
    )
    ev_net = ev.get("net_ev_pct")
    if is_live_market and ev_net is not None and float(ev_net) < cfg.min_ev_pct:
        if not (watch_ready or ignition_ready):
            return None
        alert_stage = "ignition" if ignition_ready and not watch_ready else "watch"
    if market == "EU" and window == "eu_catalyst_only":
        catalyst_score = _eu_catalyst_score(item, signals)
        if catalyst_score < 0.35:
            return None

    validity_minutes = 45 if alert_stage in WATCH_ALERT_STAGES and market == "US" else (15 if market == "US" else 12)
    valid_until = now_cet.astimezone(timezone.utc) + timedelta(minutes=validity_minutes)
    time_exit = now_cet.replace(hour=20, minute=55, second=0, microsecond=0) if market == "US" else now_cet.replace(hour=16, minute=45, second=0, microsecond=0)
    rationale_bits = [
        f"{grade} setup",
        f"VWAP {signals.get('vwap_deviation', {}).get('score', 0):+.2f}",
        f"MACD {signals.get('macd_crossover', {}).get('score', 0):+.2f}",
        f"RS {signals.get('relative_strength', {}).get('score', 0):+.2f}",
        f"ORB {signals.get('orb', {}).get('score', 0):+.2f}",
        f"1h {trend_1h.get('direction', trend_1h.get('status'))}",
        f"window {window}",
    ]
    record = {
        "market": market,
        "mode": mode,
        "status": "sent" if mode == "live" and alert_stage == "trade" else (
            "skipped" if mode == "live" else "shadow_logged"
        ),
        "alert_stage": alert_stage,
        "data_symbol": symbol,
        "broker_display_name": item.get("broker_display_name"),
        "exchange": item.get("exchange"),
        "currency": item.get("currency", "EUR"),
        "listing_type": listing_type,
        "primary_symbol": primary_symbol,
        "origin_market": item.get("origin_market"),
        "side": side,
        # Ticker metadata — carried through for display and gate logic
        "category": item.get("category"),
        "priority": item.get("priority", "medium"),
        "trade_target": item.get("trade_target", True),
        "benchmark_only": item.get("benchmark_only", False),
        "broker_tags": item.get("broker_tags", []),
        "liquidity_note": item.get("liquidity_note"),
        "grade": grade,
        "composite_score": round(composite, 4),
        "ev_net_pct": ev_net,
        "breakout_quality": breakout,
        "confidence": ev.get("confidence", 0.0),
        "valid_until": valid_until.isoformat(),
        "time_exit_at": time_exit.astimezone(timezone.utc).isoformat(),
        "valid_until_cet": valid_until.astimezone(now_cet.tzinfo).strftime("%H:%M Berlin"),
        "time_exit_cet": time_exit.strftime("%H:%M Berlin"),
        "rationale": ", ".join(rationale_bits),
        "signal_json": _compact_signal_payload(signal_result),
        "market_context_json": _market_context(market),
        "data_quality_json": quality,
        "late_chase_json": late_chase or {},
        "ignition_json": ignition or {},
        "trend_1h_json": trend_1h,
        "pullback_confirmed": False,
        "fx_rate": cfg.fx_rate,
        "fx_rate_source": cfg.fx_rate_source,
        "fx_rate_fetched_at": cfg.fx_rate_fetched_at,
        **plan,
    }
    display_levels = _display_levels(record)
    record["signal_json"] = {
        **record.get("signal_json", {}),
        "alert_stage": alert_stage,
        "late_chase": late_chase or {},
        "ignition": ignition or {},
        "trend_1h": trend_1h,
        "premium_setup": premium_setup,
        "grade_cap": grade_cap or {},
        "display": display_levels,
    }
    if listing_type == "eu_us_mirror":
        record["signal_json"] = {
            **record.get("signal_json", {}),
            "listing_type": listing_type,
            "primary_symbol": primary_symbol,
            "origin_market": item.get("origin_market", "US"),
        }
    record["message_text"] = _format_trade_card(record)
    return record


def run_advisory_cycle() -> dict:
    cycle_started = time.perf_counter()
    cfg = load_config()
    now_cet = _now_cet()
    recent_live = get_recent_advisory_signals(days=1, mode="live")
    recent_shadow = get_recent_advisory_signals(days=1, mode="shadow")
    now_utc = now_cet.astimezone(timezone.utc)
    live_sent_today = len([
        s for s in recent_live
        if str(s.get("status")) in LIVE_SIGNAL_STATUSES
    ])
    daily_live_pnl = sum(float(s.get("manual_pnl_eur") or 0) for s in recent_live)
    open_live_count = len([
        s for s in recent_live
        if _is_open_live_signal(s, now_utc)
    ])
    active_window = {market: _window_name(market, now_cet) for market in cfg.markets}
    live_sent_this_window = len([
        s for s in recent_live
        if str(s.get("status")) in LIVE_SIGNAL_STATUSES
        and active_window.get(str(s.get("market", "")).upper())
        and _is_signal_in_current_session(s, now_cet)
    ])
    watch_counts_by_symbol, live_watch_this_window = _watch_signal_counts_in_session(recent_live, now_cet)
    max_watch_per_symbol = _env_int("ADVISORY_MAX_WATCH_ALERTS_PER_SYMBOL_PER_SESSION", 4)
    max_watch_per_session = _env_int("ADVISORY_MAX_WATCH_ALERTS_PER_SESSION", 12)
    shadow_discord_sent_today = len([
        s for s in recent_shadow
        if str(s.get("market", "")).upper() in cfg.shadow_discord_markets
        and _meets_min_grade(s.get("grade"), cfg.min_discord_grade)
    ])
    recent_trades = get_recent_trades(days=90)

    # Build EU prior-composite cache from already-fetched recent_shadow (zero extra DB calls).
    # First occurrence per symbol is most-recent because get_recent_advisory_signals orders DESC.
    # Used by the EU shadow early gate below to skip full compute on tickers that were flat
    # in the last cycle.  Default to None (= unknown = don't skip) for first-run-of-day safety.
    eu_prior_composite: dict[str, float] = {}
    eu_gate_lookback = timedelta(
        minutes=max(1, _env_int("ADVISORY_EU_EARLY_GATE_LOOKBACK_MINUTES", 10))
    )
    eu_gate_cutoff = now_utc - eu_gate_lookback
    for _s in (recent_shadow or []):
        _sym = str(_s.get("data_symbol") or "").upper()
        _created_at = _parse_dt(_s.get("created_at"))
        if _sym and _sym not in eu_prior_composite and _created_at and _created_at >= eu_gate_cutoff:
            eu_prior_composite[_sym] = abs(float(_s.get("composite_score") or 0))

    emitted = []
    blocked = []
    exit_alerts = []
    open_advisory_positions = []
    discord_sent_this_cycle: set[tuple[str, str]] = set()
    first_live_discord_elapsed_s = None
    immediate_live_sent = 0
    # Only run exit monitor when this cycle is actually scanning a live market.
    # Prevents duplicate Discord exit alerts when EU and US workflows run in
    # parallel — the EU workflow has cfg.live_markets={"US"} but cfg.markets={"EU"},
    # so the intersection is empty and it correctly skips exit monitoring.
    if cfg.live_markets & cfg.markets:
        try:
            exit_alerts = _monitor_open_positions(cfg, now_cet)
        except Exception as exc:
            log_event("WARN", "advisory_exit_monitor_failed", {"error": str(exc)[:160]})
        try:
            virtual_exit_alerts = _monitor_virtual_positions(cfg, now_cet)
            exit_alerts.extend(virtual_exit_alerts)
        except Exception as exc:
            log_event("WARN", "advisory_virtual_exit_monitor_failed", {"error": str(exc)[:160]})
        try:
            open_advisory_positions = get_open_advisory_positions(max_age_days=7)
        except Exception as exc:
            log_event("WARN", "advisory_open_positions_read_failed", {"error": str(exc)[:160]})

    try:
        from backend.signals.engine import prefetch_newsapi_batch
        all_advisory_tickers = [
            item.get("primary_symbol") or item["data_symbol"]
            for market in cfg.markets
            for item in ADVISORY_UNIVERSE.get(market, [])
        ]
        prefetch_newsapi_batch(all_advisory_tickers)
    except Exception:
        pass

    def _persist_emit_candidate(candidate: dict, mode: str, *, immediate: bool = False) -> bool:
        nonlocal live_sent_today, live_sent_this_window, open_live_count
        nonlocal live_watch_this_window, shadow_discord_sent_today
        nonlocal first_live_discord_elapsed_s, immediate_live_sent

        symbol_key = (
            str(candidate.get("market", "")).upper(),
            str(candidate.get("data_symbol", "")).upper(),
        )
        if mode == "live" and candidate.get("alert_stage") in WATCH_ALERT_STAGES:
            if watch_counts_by_symbol.get(symbol_key, 0) >= max_watch_per_symbol:
                log_event("INFO", "advisory_watch_blocked_symbol_cap", {
                    "symbol": symbol_key[1],
                    "market": symbol_key[0],
                    "max_watch_per_symbol": max_watch_per_symbol,
                })
                return False
            if live_watch_this_window >= max_watch_per_session:
                log_event("INFO", "advisory_watch_blocked_session_cap", {
                    "max_watch_per_session": max_watch_per_session,
                })
                return False

        saved = insert_advisory_signal(candidate)
        if "error" not in saved and saved.get("id"):
            candidate["id"] = saved.get("id")
            candidate["message_text"] = _format_trade_card(candidate)
            if candidate.get("mode") == "live" and candidate.get("alert_stage") == "trade":
                update_advisory_exit_status(saved["id"], {"message_text": candidate["message_text"]})

        can_send_shadow = (
            mode != "shadow"
            or shadow_discord_sent_today < cfg.max_shadow_discord_alerts_per_day
        )
        can_send_discord = (
            can_send_shadow
            and _should_send_discord(candidate, cfg)
            and "error" not in saved
            and symbol_key not in discord_sent_this_cycle
        )
        if can_send_discord:
            _send_discord(candidate["message_text"], cfg.discord_webhook_url)
            discord_sent_this_cycle.add(symbol_key)
            if mode == "live" and first_live_discord_elapsed_s is None:
                first_live_discord_elapsed_s = round(time.perf_counter() - cycle_started, 3)
            if mode == "shadow":
                shadow_discord_sent_today += 1
            if immediate:
                immediate_live_sent += 1

        if mode == "live" and candidate.get("status") in LIVE_SIGNAL_STATUSES and "error" not in saved:
            live_sent_today += 1
            live_sent_this_window += 1
            open_live_count += 1
        if mode == "live" and candidate.get("alert_stage") in WATCH_ALERT_STAGES and "error" not in saved:
            watch_counts_by_symbol[symbol_key] = watch_counts_by_symbol.get(symbol_key, 0) + 1
            live_watch_this_window += 1

        # Auto-create virtual position for live A/A+ trade-stage alerts
        if (
            mode == "live"
            and candidate.get("alert_stage") == "trade"
            and candidate.get("grade") in {"A", "A+"}
            and "error" not in saved
            and not candidate.get("downside_risk")
        ):
            try:
                _candidate_market = str(candidate.get("market", "")).upper()
                virt_entry = (
                    ((float(candidate.get("entry_min") or 0) + float(candidate.get("entry_max") or 0)) / 2.0)
                    or float(candidate.get("reference_price") or 0)
                )
                create_virtual_position({
                    "advisory_signal_id": saved.get("id"),
                    "data_symbol": candidate["data_symbol"],
                    "market": _candidate_market,
                    "side": candidate.get("side", "BUY"),
                    "window": candidate.get("window"),
                    "grade": candidate.get("grade"),
                    "entry_price_native": round(virt_entry, 4) if virt_entry else None,
                    "stop_price": candidate.get("stop_price"),
                    "target_1": candidate.get("target_1"),
                    "target_2": candidate.get("target_2"),
                    "currency": candidate.get("currency", "USD"),
                    "fx_rate": candidate.get("fx_rate") or cfg.fx_rate,
                    "suggested_size_eur": candidate.get("suggested_size_eur"),
                    "status": "open",
                })
            except Exception:
                pass

        emitted.append(candidate)
        return "error" not in saved

    market_timings = []

    for market in _ordered_markets(cfg):
        market_started = time.perf_counter()
        if market not in ADVISORY_UNIVERSE:
            continue
        mode = "live" if market in cfg.live_markets else "shadow"
        if market not in cfg.live_markets and market not in cfg.shadow_markets:
            continue
        window = _window_name(market, now_cet)
        if mode == "live" and market == "US" and window == "us_open":
            minutes_since_open = _minutes_since_session_start(market, window, now_cet)
            if minutes_since_open < cfg.us_min_minutes_after_open:
                log_event("INFO", "advisory_live_waiting_for_us_open_bars", {
                    "minutes_since_open": round(minutes_since_open, 1),
                    "required_minutes": cfg.us_min_minutes_after_open,
                })
                continue
        if mode == "live" and daily_live_pnl <= -abs(cfg.max_daily_loss_eur):
            log_event("INFO", "advisory_live_blocked_daily_loss_cap", {
                "daily_live_pnl": daily_live_pnl,
                "max_daily_loss_eur": cfg.max_daily_loss_eur,
            })
            continue
        if mode == "live" and open_live_count >= cfg.max_open_live_trades:
            log_event("INFO", "advisory_live_blocked_open_trade_cap", {
                "open_live_count": open_live_count,
                "max_open_live_trades": cfg.max_open_live_trades,
            })
            continue
        market_candidates = []
        _priority_rank = {"high": 0, "medium": 1, "low": 2}
        _sorted_items = sorted(
            ADVISORY_UNIVERSE[market],
            key=lambda it: _priority_rank.get(str(it.get("priority", "medium")), 1),
        )
        scanned_count = 0
        high_priority_count = len([
            it for it in _sorted_items
            if str(it.get("priority", "medium")).lower() == "high"
        ])
        high_priority_logged = high_priority_count == 0
        for item in _sorted_items:
            if (
                not high_priority_logged
                and str(item.get("priority", "medium")).lower() != "high"
            ):
                log_event("INFO", "advisory_high_priority_scan_complete", {
                    "market": market,
                    "elapsed_s": round(time.perf_counter() - market_started, 3),
                    "cycle_elapsed_s": round(time.perf_counter() - cycle_started, 3),
                    "high_priority_count": high_priority_count,
                    "scanned": scanned_count,
                    "immediate_live_sent": immediate_live_sent,
                })
                high_priority_logged = True

            scanned_count += 1
            # EU shadow early gate: skip full signal compute for tickers whose prior composite
            # was very low, indicating no momentum.  Controlled by env var; default 0.15.
            # Only applies to non-mirror EU shadow tickers (mirrors have their own window gate).
            if (
                mode == "shadow"
                and market == "EU"
                and item.get("listing_type") != "eu_us_mirror"
            ):
                _sym_key = item["data_symbol"].upper()
                _prior_c = eu_prior_composite.get(_sym_key)
                _gate = _env_float("ADVISORY_EU_EARLY_GATE_COMPOSITE", 0.15)
                if _prior_c is not None and _prior_c < _gate:
                    log_event("DEBUG", "advisory_eu_early_gate_skip", {
                        "symbol": _sym_key, "prior_composite": round(_prior_c, 4), "gate": _gate,
                    })
                    continue

            if mode == "live" and _alerted_symbol_in_session(
                recent_live, item["data_symbol"], market, now_cet
            ):
                try:
                    insert_advisory_scan_log({
                        "data_symbol": item["data_symbol"],
                        "primary_symbol": item.get("primary_symbol"),
                        "market": market,
                        "window": window,
                        "listing_type": item.get("listing_type"),
                        "alerted": False,
                        "gate_reason": "already_alerted_session",
                    })
                except Exception:
                    pass
                continue
            recent_watch = None
            if mode == "live":
                recent_watch = _recent_watch_signal_in_session(
                    recent_live, item["data_symbol"], market, now_cet
                )
            candidate = _scan_candidate(item, market, mode, cfg, recent_trades, now_cet)

            # --- build scan log base ---
            _scan_log_base = {
                "data_symbol": item["data_symbol"],
                "primary_symbol": item.get("primary_symbol"),
                "market": market,
                "window": window,
                "listing_type": item.get("listing_type"),
                "alerted": False,
                "gate_reason": "no_candidate" if not candidate else (
                    candidate.get("status", "") if candidate.get("status", "").startswith("blocked") else None
                ),
            }
            if candidate:
                _scan_log_base.update({
                    "composite_score": candidate.get("composite_score"),
                    "grade": candidate.get("grade"),
                    "side": candidate.get("side"),
                    "alert_stage": candidate.get("alert_stage"),
                    "ev_net_pct": candidate.get("ev_net_pct"),
                    "breakout_quality": candidate.get("breakout_quality"),
                    "price_native": candidate.get("reference_price") or (candidate.get("signal_json") or {}).get("atr_data", {}).get("current_price"),
                    "downside_risk": candidate.get("downside_risk", False),
                    "gate_detail": candidate.get("block_detail") or {},
                })
                _sig_scores = (candidate.get("signal_json") or {}).get("signals") or {}
                _scan_log_base.update({
                    "vwap_score": (_sig_scores.get("vwap_deviation") or {}).get("score"),
                    "macd_score": (_sig_scores.get("macd_crossover") or {}).get("score"),
                    "rel_strength_score": (_sig_scores.get("relative_strength") or {}).get("score"),
                    "tape_score": (_sig_scores.get("tape_aggression") or {}).get("score"),
                    "rsi_score": (_sig_scores.get("rsi_divergence") or {}).get("score"),
                    "orb_active": bool((_sig_scores.get("orb") or {}).get("meta", {}).get("active")),
                })

            if not candidate:
                try:
                    insert_advisory_scan_log(_scan_log_base)
                except Exception:
                    pass
                continue
            if candidate.get("benchmark_only") or not candidate.get("trade_target", True):
                candidate["status"] = "benchmark_logged"
                insert_advisory_signal(candidate)
                emitted.append(candidate)
                _scan_log_base["gate_reason"] = "benchmark_only"
                try:
                    insert_advisory_scan_log(_scan_log_base)
                except Exception:
                    pass
                continue
            if mode == "live":
                runner = _runner_context(candidate, recent_live, now_cet, open_advisory_positions, cfg)
                if runner:
                    candidate["runner_context"] = runner
                    if runner.get("holder_context"):
                        candidate["holding_context"] = runner["holder_context"]
                    candidate["signal_json"] = {
                        **candidate.get("signal_json", {}),
                        "runner_context": runner,
                    }
                    candidate["message_text"] = _format_trade_card(candidate)
            if mode == "live" and _watch_repeat_blocked(recent_watch, candidate):
                _scan_log_base["gate_reason"] = "watch_repeat_blocked"
                try:
                    insert_advisory_scan_log(_scan_log_base)
                except Exception:
                    pass
                continue
            if mode == "live" and candidate.get("alert_stage") == "trade" and _watch_had_late_chase(recent_watch):
                candidate["pullback_confirmed"] = True
                candidate["signal_json"] = {
                    **candidate.get("signal_json", {}),
                    "pullback_confirmed": True,
                }
                candidate["message_text"] = _format_trade_card(candidate)
            if candidate.get("status", "").startswith("blocked"):
                insert_advisory_signal(candidate)
                blocked.append(candidate)
                _scan_log_base["gate_reason"] = candidate.get("status")
                try:
                    insert_advisory_scan_log(_scan_log_base)
                except Exception:
                    pass
                continue
            if (
                mode == "live"
                and str(candidate.get("priority", "medium")).lower() == "high"
                and _should_send_discord(candidate, cfg)
                and min(
                    cfg.max_live_alerts_per_day - live_sent_today,
                    cfg.max_live_trades_per_session - live_sent_this_window,
                ) > 0
            ):
                _emitted_ok = _persist_emit_candidate(candidate, mode, immediate=True)
                _scan_log_base["alerted"] = _emitted_ok
                _scan_log_base["gate_reason"] = "alerted" if _emitted_ok else "emit_failed"
                try:
                    insert_advisory_scan_log(_scan_log_base)
                except Exception:
                    pass
                continue
            market_candidates.append(candidate)
            # scan log for queued candidates is written after emit decision below

        if not high_priority_logged:
            log_event("INFO", "advisory_high_priority_scan_complete", {
                "market": market,
                "elapsed_s": round(time.perf_counter() - market_started, 3),
                "cycle_elapsed_s": round(time.perf_counter() - cycle_started, 3),
                "high_priority_count": high_priority_count,
                "scanned": scanned_count,
                "immediate_live_sent": immediate_live_sent,
            })

        market_candidates.sort(
            key=lambda c: (
                ALERT_STAGE_RANK.get(str(c.get("alert_stage") or ""), -1),
                c.get("grade") == "A+",
                float(c.get("ev_net_pct") or 0),
                float(c.get("breakout_quality") or 0),
                float(c.get("composite_score") or 0),
            ),
            reverse=True,
        )
        if mode == "live":
            day_limit = cfg.max_live_alerts_per_day - live_sent_today
            session_limit = cfg.max_live_trades_per_session - live_sent_this_window
            limit = min(day_limit, session_limit)
        else:
            limit = cfg.max_shadow_signals_per_day
        for idx, candidate in enumerate(market_candidates):
            will_emit = idx < max(0, limit)
            _emitted_ok = _persist_emit_candidate(candidate, mode) if will_emit else False
            try:
                _sig_scores_q = (candidate.get("signal_json") or {}).get("signals") or {}
                insert_advisory_scan_log({
                    "data_symbol": candidate.get("data_symbol"),
                    "primary_symbol": candidate.get("primary_symbol"),
                    "market": market,
                    "window": window,
                    "listing_type": candidate.get("listing_type"),
                    "composite_score": candidate.get("composite_score"),
                    "grade": candidate.get("grade"),
                    "side": candidate.get("side"),
                    "alert_stage": candidate.get("alert_stage"),
                    "alerted": _emitted_ok,
                    "gate_reason": "alerted" if _emitted_ok else ("capped_by_limit" if not will_emit else "emit_failed"),
                    "ev_net_pct": candidate.get("ev_net_pct"),
                    "breakout_quality": candidate.get("breakout_quality"),
                    "price_native": candidate.get("reference_price") or (candidate.get("signal_json") or {}).get("atr_data", {}).get("current_price"),
                    "downside_risk": candidate.get("downside_risk", False),
                    "gate_detail": candidate.get("block_detail") or {},
                    "vwap_score": (_sig_scores_q.get("vwap_deviation") or {}).get("score"),
                    "macd_score": (_sig_scores_q.get("macd_crossover") or {}).get("score"),
                    "rel_strength_score": (_sig_scores_q.get("relative_strength") or {}).get("score"),
                    "tape_score": (_sig_scores_q.get("tape_aggression") or {}).get("score"),
                    "rsi_score": (_sig_scores_q.get("rsi_divergence") or {}).get("score"),
                    "orb_active": bool((_sig_scores_q.get("orb") or {}).get("meta", {}).get("active")),
                })
            except Exception:
                pass

        market_timings.append({
            "market": market,
            "mode": mode,
            "scanned": scanned_count,
            "elapsed_s": round(time.perf_counter() - market_started, 3),
        })

    total_elapsed_s = round(time.perf_counter() - cycle_started, 3)
    log_event("INFO", "advisory_cycle_timing", {
        "total_elapsed_s": total_elapsed_s,
        "first_live_discord_elapsed_s": first_live_discord_elapsed_s,
        "immediate_live_sent": immediate_live_sent,
        "market_timings": market_timings,
        "markets": sorted(cfg.markets),
    })
    log_event("INFO", "advisory_cycle_complete", {
        "emitted": len(emitted),
        "blocked": len(blocked),
        "exit_alerts": len(exit_alerts),
        "live_sent_today": live_sent_today,
        "daily_live_pnl": daily_live_pnl,
        "markets": sorted(cfg.markets),
        "total_elapsed_s": total_elapsed_s,
        "first_live_discord_elapsed_s": first_live_discord_elapsed_s,
        "immediate_live_sent": immediate_live_sent,
    })
    return {
        "emitted": len(emitted),
        "blocked": len(blocked),
        "exit_alerts": len(exit_alerts),
        "live_sent_today": live_sent_today,
    }
