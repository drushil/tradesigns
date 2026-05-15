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
from backend.learning.engine import compute_expected_value
from database.client import (
    get_recent_trades,
    get_recent_advisory_signals,
    insert_advisory_signal,
    log_event,
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

LIVE_SIGNAL_STATUSES = {"sent", "entered", "hit_stop", "hit_target"}
OPEN_LIVE_STATUSES = {"sent", "entered"}


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
    min_ev_pct: float
    min_breakout_quality: float
    min_discord_grade: str
    allow_short: bool
    discord_webhook_url: str
    fx_rate: float


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


def load_config() -> AdvisoryConfig:
    markets = _csv_set("ADVISORY_MARKETS", "US,EU")
    live = _csv_set("ADVISORY_LIVE_MARKETS", "US")
    shadow = _csv_set("ADVISORY_SHADOW_MARKETS", "EU")
    shadow_discord = _csv_set("ADVISORY_SHADOW_DISCORD_MARKETS", "OFF")
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
        min_ev_pct=_env_float("ADVISORY_MIN_EV_PCT", 0.50),
        min_breakout_quality=_env_float("ADVISORY_MIN_BREAKOUT_QUALITY", 0.45),
        min_discord_grade=_env_value("ADVISORY_DISCORD_MIN_GRADE", "A").upper(),
        allow_short=_env_bool("ADVISORY_ALLOW_SHORT", False),
        discord_webhook_url=_env_value("DISCORD_WEBHOOK_URL", ""),
        fx_rate=_env_float("EURUSD_RATE", 1.08),
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
        if 15 * 60 + 30 <= minutes <= 17 * 60:
            return "us_open"
        if 20 * 60 <= minutes <= 21 * 60:
            return "us_afternoon"
        return None
    return None


def _session_start_cet(market: str, window: str, now_cet: datetime) -> datetime:
    starts = {
        "EU": {"eu_open": (9, 15), "eu_catalyst_only": (14, 0)},
        "US": {"us_open": (15, 30), "us_afternoon": (20, 0)},
    }
    hour, minute = starts.get(market, {}).get(window, (now_cet.hour, now_cet.minute))
    return now_cet.replace(hour=hour, minute=minute, second=0, microsecond=0)


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


def _data_quality(symbol: str, market: str) -> dict:
    try:
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
        min_rows = 45 if market == "EU" else 30
        if rows < min_rows:
            return {"ok": False, "reason": "too_few_bars", "rows": rows}
        if age_min > 20:
            return {"ok": False, "reason": "stale_bars", "age_minutes": round(age_min, 1), "rows": rows}
        if avg_volume <= 0:
            return {"ok": False, "reason": "zero_recent_volume", "rows": rows}
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
    cur = _currency_symbol(signal["currency"])
    is_shadow = signal["mode"] != "live"
    mode = "LIVE" if not is_shadow else "SHADOW OBSERVATION"
    valid = signal["valid_until_cet"]
    time_exit = signal["time_exit_cet"]
    approx_usd = signal["suggested_size_eur"] * signal["fx_rate"] if signal["currency"] == "USD" else None
    notional = f"€{signal['suggested_size_eur']:.0f}"
    if approx_usd:
        notional += f" (~${approx_usd:.0f})"
    fx_text = f"  |  FX: {signal['fx_rate']:.4f}" if signal["currency"] == "USD" else ""
    opportunity = "EXCELLENT" if signal["grade"] == "A+" else "VERY GOOD"
    quick_why = signal["rationale"].split(", window ")[0]
    headline = (
        f"{opportunity} {signal['side']} OPPORTUNITY: {sym} / {name} "
        f"because {quick_why}."
    )
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
    return (
        f"{first_line}\n"
        f"**{headline}**\n"
        f"{shadow_note}"
        f"{quick_label}: LIMIT {signal['side']} {sym} "
        f"{cur}{signal['entry_min']:.2f}-{cur}{signal['entry_max']:.2f}; "
        f"do not chase > {cur}{signal['do_not_chase_price']:.2f}; valid until {valid}.\n"
        f"Size/risk: {notional} max, ~€{signal['risk_eur']:.0f} risk{fx_text}.\n"
        f"Exit: stop {cur}{signal['stop_price']:.2f}; T1 {cur}{signal['target_1']:.2f}; "
        f"T2 {cur}{signal['target_2']:.2f}; time exit {time_exit}.\n"
        f"Why: {signal['rationale']}.\n"
        f"Grade: **{signal['grade']}**  |  Composite: {signal['composite_score']:.2f}  "
        f"|  EV: {signal['ev_net_pct'] if signal['ev_net_pct'] is not None else 'n/a'}%"
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


def _weights_for_market(market: str) -> dict:
    if market == "EU":
        return EU_WEIGHTS
    return get_profile(_env_value("RISK_PROFILE", "moderate")).get("signal_weights", {})


def _should_send_discord(candidate: dict, cfg: AdvisoryConfig) -> bool:
    if not _meets_min_grade(candidate.get("grade"), cfg.min_discord_grade):
        return False
    if candidate.get("mode") == "live":
        return True
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
    quality = _data_quality(symbol, market)
    if not quality.get("ok"):
        return {
            "market": market, "mode": mode, "status": "blocked_data_quality",
            "data_symbol": symbol, "broker_display_name": item.get("broker_display_name"),
            "exchange": item.get("exchange"), "currency": item.get("currency"),
            "side": "BUY", "data_quality_json": quality,
            "rationale": f"Data quality blocked: {quality.get('reason')}",
        }

    regime_state = detect_regime(symbol)
    weights = _weights_for_market(market)
    signal_result = compute_all_signals(symbol, weights, regime_state=regime_state)
    composite = float(signal_result.get("composite_score") or 0)
    if composite <= 0 or (composite < cfg.min_composite and market in cfg.live_markets):
        return None
    side = "BUY" if composite > 0 else "SELL"
    if side == "SELL" and not cfg.allow_short:
        return None

    signals = signal_result.get("signals") or {}
    breakout = _breakout_quality(side, composite, signals, getattr(regime_state, "market_regime", ""))
    orb_active = bool((signals.get("orb") or {}).get("meta", {}).get("active"))
    grade = _grade(composite, breakout, orb_active)
    if market in cfg.live_markets and grade not in {"A+", "A"}:
        return None
    if market in cfg.live_markets and breakout < cfg.min_breakout_quality:
        return None

    atr_data = signal_result.get("atr_data") or {}
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
    if market in cfg.live_markets and ev_net is not None and float(ev_net) < cfg.min_ev_pct:
        return None
    if market == "EU" and window == "eu_catalyst_only":
        catalyst_score = _eu_catalyst_score(item, signals)
        if catalyst_score < 0.35:
            return None

    valid_until = now_cet.astimezone(timezone.utc) + timedelta(minutes=15 if market == "US" else 12)
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
        "status": "sent" if mode == "live" else "shadow_logged",
        "data_symbol": symbol,
        "broker_display_name": item.get("broker_display_name"),
        "exchange": item.get("exchange"),
        "currency": item.get("currency", "EUR"),
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
        "fx_rate": cfg.fx_rate,
        **plan,
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
            item["data_symbol"]
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
            candidate = _scan_candidate(item, market, mode, cfg, recent_trades, now_cet)
            if not candidate:
                continue
            if candidate.get("status", "").startswith("blocked"):
                insert_advisory_signal(candidate)
                blocked.append(candidate)
                continue
            market_candidates.append(candidate)

        market_candidates.sort(
            key=lambda c: (
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
            saved = insert_advisory_signal(candidate)
            can_send_shadow = (
                mode != "shadow"
                or shadow_discord_sent_today < cfg.max_shadow_discord_alerts_per_day
            )
            if can_send_shadow and _should_send_discord(candidate, cfg) and "error" not in saved:
                _send_discord(candidate["message_text"], cfg.discord_webhook_url)
                if mode == "shadow":
                    shadow_discord_sent_today += 1
            if mode == "live" and "error" not in saved:
                live_sent_today += 1
                live_sent_this_window += 1
                open_live_count += 1
            emitted.append(candidate)

    log_event("INFO", "advisory_cycle_complete", {
        "emitted": len(emitted),
        "blocked": len(blocked),
        "live_sent_today": live_sent_today,
        "daily_live_pnl": daily_live_pnl,
        "markets": sorted(cfg.markets),
    })
    return {"emitted": len(emitted), "blocked": len(blocked), "live_sent_today": live_sent_today}
