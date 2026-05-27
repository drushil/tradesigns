"""
Manual trading advisory mode.

This module reuses the signal engine but never submits broker orders. It logs
high-conviction suggestions to advisory_signals and sends live US trade cards
to Discord while EU runs in shadow/observation mode by default.
"""
from __future__ import annotations

import os
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
    get_fx_rate_cache,
    insert_advisory_signal,
    log_event,
    upsert_fx_rate_cache,
)


ADVISORY_UNIVERSE = {
    "US": [
        {"data_symbol": "NVDA", "broker_display_name": "NVIDIA", "exchange": "NASDAQ", "currency": "USD"},
        {"data_symbol": "AMD", "broker_display_name": "AMD", "exchange": "NASDAQ", "currency": "USD"},
        {"data_symbol": "AAPL", "broker_display_name": "Apple", "exchange": "NASDAQ", "currency": "USD"},
        {"data_symbol": "MSFT", "broker_display_name": "Microsoft", "exchange": "NASDAQ", "currency": "USD"},
        {"data_symbol": "META", "broker_display_name": "Meta Platforms", "exchange": "NASDAQ", "currency": "USD"},
        {"data_symbol": "AMZN", "broker_display_name": "Amazon", "exchange": "NASDAQ", "currency": "USD"},
        {"data_symbol": "TSLA", "broker_display_name": "Tesla", "exchange": "NASDAQ", "currency": "USD"},
        {"data_symbol": "QQQ", "broker_display_name": "Invesco QQQ", "exchange": "NASDAQ", "currency": "USD"},
        {"data_symbol": "SPY", "broker_display_name": "SPDR S&P 500 ETF", "exchange": "NYSE Arca", "currency": "USD"},
    ],
    "EU": [
        {"data_symbol": "ASML.AS", "broker_display_name": "ASML", "exchange": "Euronext Amsterdam", "currency": "EUR"},
        {"data_symbol": "SAP.DE", "broker_display_name": "SAP", "exchange": "Xetra", "currency": "EUR"},
        {"data_symbol": "SIE.DE", "broker_display_name": "Siemens", "exchange": "Xetra", "currency": "EUR"},
        {"data_symbol": "AIR.PA", "broker_display_name": "Airbus", "exchange": "Euronext Paris", "currency": "EUR"},
        {"data_symbol": "MC.PA", "broker_display_name": "LVMH", "exchange": "Euronext Paris", "currency": "EUR"},
        {"data_symbol": "ALV.DE", "broker_display_name": "Allianz", "exchange": "Xetra", "currency": "EUR"},
        {"data_symbol": "DTE.DE", "broker_display_name": "Deutsche Telekom", "exchange": "Xetra", "currency": "EUR"},
        {"data_symbol": "IFX.DE", "broker_display_name": "Infineon", "exchange": "Xetra", "currency": "EUR"},
        {"data_symbol": "NVD.DE", "broker_display_name": "NVIDIA (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "NVDA", "mirror_only_windows": ["eu_open"]},
        {"data_symbol": "AMD.DE", "broker_display_name": "AMD (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "AMD", "mirror_only_windows": ["eu_open"]},
        {"data_symbol": "APC.DE", "broker_display_name": "Apple (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "AAPL", "mirror_only_windows": ["eu_open"]},
        {"data_symbol": "MSF.DE", "broker_display_name": "Microsoft (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "MSFT", "mirror_only_windows": ["eu_open"]},
        {"data_symbol": "AMZ.DE", "broker_display_name": "Amazon (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "AMZN", "mirror_only_windows": ["eu_open"]},
        {"data_symbol": "TL0.DE", "broker_display_name": "Tesla (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "TSLA", "mirror_only_windows": ["eu_open"]},
        {"data_symbol": "FB2A.DE", "broker_display_name": "Meta (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "META", "mirror_only_windows": ["eu_open"]},
        {"data_symbol": "ABEA.DE", "broker_display_name": "Alphabet A (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "GOOGL", "mirror_only_windows": ["eu_open"]},
        {"data_symbol": "PTX.DE", "broker_display_name": "Palantir (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "PLTR", "mirror_only_windows": ["eu_open"]},
        {"data_symbol": "NFC.DE", "broker_display_name": "Netflix (Xetra)", "exchange": "Xetra", "currency": "EUR",
         "origin_market": "US", "listing_type": "eu_us_mirror", "primary_symbol": "NFLX", "mirror_only_windows": ["eu_open"]},
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
WATCH_ALERT_STAGES = {"watch", "ignition"}
ALERT_STAGE_RANK = {"ignition": 0, "watch": 1, "trade": 2}


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


def _fetch_latest_eurusd_rate() -> Optional[float]:
    """Fetch EUR/USD once through the existing bars data path."""
    try:
        if _get_bars is not None:
            bars = _get_bars("EURUSD=X", period="5d", interval="1d")
        else:
            import yfinance as yf
            bars = yf.download("EURUSD=X", period="5d", interval="1d", progress=False, auto_adjust=True)
        if bars is None or bars.empty:
            return None
        close = bars["Close"].squeeze()
        if hasattr(close, "dropna"):
            rate = float(close.dropna().iloc[-1])
        else:
            rate = float(close)
        if 0.8 <= rate <= 1.4:
            return rate
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
        if 9 * 60 + 15 <= minutes <= 11 * 60:
            return "eu_open"
        if 14 * 60 <= minutes <= 16 * 60:
            return "eu_catalyst_only"
        return None
    if market == "US":
        if 15 * 60 <= minutes < 15 * 60 + 30:
            return "us_premarket"
        if 15 * 60 + 30 <= minutes <= 17 * 60:
            return "us_open"
        if 20 * 60 <= minutes <= 21 * 60:
            return "us_afternoon"
        return None
    return None


def _session_start_cet(market: str, window: str, now_cet: datetime) -> datetime:
    starts = {
        "EU": {"eu_open": (9, 15), "eu_catalyst_only": (14, 0)},
        "US": {"us_premarket": (15, 0), "us_open": (15, 30), "us_afternoon": (20, 0)},
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


def _watch_repeat_blocked(recent_watch: dict, candidate: dict) -> bool:
    if not recent_watch or candidate.get("alert_stage") not in WATCH_ALERT_STAGES:
        return False
    signal_json = recent_watch.get("signal_json") or {}
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


def _grade_rank(grade: str) -> int:
    return {"C": 0, "B": 1, "A": 2, "A+": 3}.get(str(grade).upper(), -1)


def _meets_min_grade(grade: str, min_grade: str) -> bool:
    return _grade_rank(grade) >= _grade_rank(min_grade)


def _data_quality(symbol: str, market: str, listing_type: str = None) -> dict:
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
        if listing_type == "eu_us_mirror" and avg_volume < 300:
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


def _format_trade_card(signal: dict) -> str:
    sym = signal["data_symbol"]
    name = signal.get("broker_display_name") or sym
    is_shadow = signal["mode"] != "live"
    is_watch = signal.get("alert_stage") in WATCH_ALERT_STAGES
    is_ignition = signal.get("alert_stage") == "ignition"
    is_late_chase = bool(signal.get("late_chase_json"))
    notional = f"€{signal['suggested_size_eur']:.0f}"
    fx_text = (
        f"  |  EUR/USD: {signal['fx_rate']:.4f} ({signal.get('fx_rate_source', 'unknown')})"
        if signal["currency"] == "USD" else ""
    )
    entry_min_eur = _eur_price(signal, "entry_min")
    entry_max_eur = _eur_price(signal, "entry_max")
    chase_eur = _eur_price(signal, "do_not_chase_price")
    stop_eur = _eur_price(signal, "stop_price")
    target_1_eur = _eur_price(signal, "target_1")
    target_2_eur = _eur_price(signal, "target_2")
    native_ref = _native_ref_line(signal)
    opportunity = {
        "A+": "EXCELLENT",
        "A": "VERY GOOD",
        "B": "WATCHLIST-QUALITY",
        "C": "LOW-GRADE SHADOW",
    }.get(str(signal.get("grade", "")).upper(), "OBSERVATION")
    if is_ignition:
        opportunity = "MOMENTUM IGNITION"
    elif is_watch:
        opportunity = "LATE-CHASE WATCH" if is_late_chase else "EARLY WATCH"
    quick_why = signal["rationale"].split(", window ")[0]
    headline = (
        f"{opportunity} {signal['side']} OPPORTUNITY: {sym} / {name} "
        f"because {quick_why}."
    )
    if is_ignition:
        first_line = f"**MOMENTUM IGNITION - {signal['market']} {signal['side']} SETUP FORMING - WATCH ONLY**"
        quick_label = "Ignition watch"
    elif is_watch:
        first_line = (
            f"**WATCH ONLY - {signal['market']} {signal['side']} SETUP "
            f"{'IS EXTENDED' if is_late_chase else 'FORMING'} - DO NOT CHASE**"
        )
        quick_label = "Watch plan"
    else:
        first_line = (
            f"**LIVE TRADE ALERT - {signal['market']} {signal['side']} NOW**"
            if not is_shadow
            else f"**SHADOW OBSERVATION - {signal['market']} {signal['side']} SETUP - DO NOT TRADE YET**"
        )
        quick_label = "Quick action" if not is_shadow else "Observation plan"
    shadow_note = (
        "This is shadow mode: log/watch only until EU advisory is promoted live.\n"
        if is_shadow else ""
    )
    mirror_note = (
        f"Pre-Nasdaq mirror of {signal.get('primary_symbol')} - "
        f"EU-hours early read on US momentum. Execute on US listing after open.\n"
        if signal.get("listing_type") == "eu_us_mirror" else ""
    )
    watch_note = ""
    if is_watch:
        if is_ignition:
            detail = signal.get("ignition_json") or {}
            watch_note = (
                "Momentum ignition detected before the full advisory composite has confirmed; "
                f"{detail.get('move_pct')}% over {detail.get('window_bars')}m, "
                f"{detail.get('atr_multiple')}x ATR move, {detail.get('volume_ratio')}x volume.\n"
            )
        elif is_late_chase:
            detail = signal.get("late_chase_json") or {}
            watch_note = (
                "Execution gate says this move is extended. Wait for a pullback into the entry band; "
                f"VWAP deviation {detail.get('pct_deviation')}% vs threshold {detail.get('threshold_pct')}%.\n"
            )
        else:
            watch_note = "Early signal only: prepare the chart and wait for confirmed A/A+ follow-through.\n"
    elif signal.get("pullback_confirmed"):
        watch_note = "Pullback confirmed: the prior late-chase extension has cleared and the entry band is now valid.\n"
    if is_watch:
        if is_ignition:
            action_line = (
                f"{quick_label}: prepare {signal['side']} {sym}; watch follow-through near "
                f"€{entry_min_eur:.2f}-€{entry_max_eur:.2f}; "
                f"valid until {signal['valid_until_cet']}.\n"
            )
        else:
            action_line = (
                f"{quick_label}: prepare {signal['side']} {sym} only on pullback into "
                f"€{entry_min_eur:.2f}-€{entry_max_eur:.2f}; "
                f"valid until {signal['valid_until_cet']}.\n"
            )
        risk_line = f"Tentative levels: max size {notional}, estimated risk ~€{signal['risk_eur']:.0f}{fx_text}.\n"
        exit_line = (
            f"Pullback plan: stop €{stop_eur:.2f}; T1 €{target_1_eur:.2f}; "
            f"T2 €{target_2_eur:.2f}; reassess by {signal['time_exit_cet']}.\n"
        )
    else:
        action_line = (
            f"{quick_label}: LIMIT {signal['side']} {sym} "
            f"€{entry_min_eur:.2f}-€{entry_max_eur:.2f}; "
            f"do not chase > €{chase_eur:.2f}; "
            f"valid until {signal['valid_until_cet']}.\n"
        )
        risk_line = f"Size/risk: {notional} max, ~€{signal['risk_eur']:.0f} risk{fx_text}.\n"
        exit_line = (
            f"Exit: stop €{stop_eur:.2f}; T1 €{target_1_eur:.2f}; "
            f"T2 €{target_2_eur:.2f}; time exit {signal['time_exit_cet']}.\n"
        )
    ev_net = signal.get("ev_net_pct")
    ev_text = "n/a" if ev_net is None else f"{float(ev_net):.2f}"
    return (
        f"{first_line}\n"
        f"**{headline}**\n"
        f"{shadow_note}"
        f"{mirror_note}"
        f"{watch_note}"
        f"{action_line}"
        f"{native_ref}"
        f"{risk_line}"
        f"{exit_line}"
        f"Why: {signal['rationale']}.\n"
        f"Grade: **{signal['grade']}**  |  Composite: {signal['composite_score']:.2f}  "
        f"|  EV: {ev_text}%"
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
    if candidate.get("alert_stage") in WATCH_ALERT_STAGES:
        return candidate.get("mode") == "live"
    if candidate.get("mode") == "live":
        if not _meets_min_grade(candidate.get("grade"), cfg.min_discord_grade):
            return False
        return True
    if not _meets_min_grade(candidate.get("grade"), cfg.shadow_min_discord_grade):
        return False
    return str(candidate.get("market", "")).upper() in cfg.shadow_discord_markets


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
    quality = _data_quality(symbol, market, listing_type=listing_type)
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
        return None
    side = "BUY" if composite > 0 else "SELL"
    if side == "SELL" and not cfg.allow_short:
        return None

    signals = signal_result.get("signals") or {}
    breakout = _breakout_quality(side, composite, signals, getattr(regime_state, "market_regime", ""))
    orb_active = bool((signals.get("orb") or {}).get("meta", {}).get("active"))
    grade = _grade(composite, breakout, orb_active)
    atr_data = signal_result.get("atr_data") or {}
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
    emitted = []
    blocked = []

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

    for market in _ordered_markets(cfg):
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
        for item in ADVISORY_UNIVERSE[market]:
            if mode == "live" and _alerted_symbol_in_session(
                recent_live, item["data_symbol"], market, now_cet
            ):
                continue
            recent_watch = None
            if mode == "live":
                recent_watch = _recent_watch_signal_in_session(
                    recent_live, item["data_symbol"], market, now_cet
                )
            candidate = _scan_candidate(item, market, mode, cfg, recent_trades, now_cet)
            if not candidate:
                continue
            if mode == "live" and _watch_repeat_blocked(recent_watch, candidate):
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
                continue
            market_candidates.append(candidate)

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
        for candidate in market_candidates[:max(0, limit)]:
            if mode == "live" and candidate.get("alert_stage") in WATCH_ALERT_STAGES:
                key = (str(candidate.get("market", "")).upper(), str(candidate.get("data_symbol", "")).upper())
                if watch_counts_by_symbol.get(key, 0) >= max_watch_per_symbol:
                    log_event("INFO", "advisory_watch_blocked_symbol_cap", {
                        "symbol": key[1],
                        "market": key[0],
                        "max_watch_per_symbol": max_watch_per_symbol,
                    })
                    continue
                if live_watch_this_window >= max_watch_per_session:
                    log_event("INFO", "advisory_watch_blocked_session_cap", {
                        "max_watch_per_session": max_watch_per_session,
                    })
                    continue
            saved = insert_advisory_signal(candidate)
            can_send_shadow = (
                mode != "shadow"
                or shadow_discord_sent_today < cfg.max_shadow_discord_alerts_per_day
            )
            if can_send_shadow and _should_send_discord(candidate, cfg) and "error" not in saved:
                _send_discord(candidate["message_text"], cfg.discord_webhook_url)
                if mode == "shadow":
                    shadow_discord_sent_today += 1
            if mode == "live" and candidate.get("status") in LIVE_SIGNAL_STATUSES and "error" not in saved:
                live_sent_today += 1
                live_sent_this_window += 1
                open_live_count += 1
            if mode == "live" and candidate.get("alert_stage") in WATCH_ALERT_STAGES and "error" not in saved:
                key = (str(candidate.get("market", "")).upper(), str(candidate.get("data_symbol", "")).upper())
                watch_counts_by_symbol[key] = watch_counts_by_symbol.get(key, 0) + 1
                live_watch_this_window += 1
            emitted.append(candidate)

    log_event("INFO", "advisory_cycle_complete", {
        "emitted": len(emitted),
        "blocked": len(blocked),
        "live_sent_today": live_sent_today,
        "daily_live_pnl": daily_live_pnl,
        "markets": sorted(cfg.markets),
    })
    return {"emitted": len(emitted), "blocked": len(blocked), "live_sent_today": live_sent_today}
