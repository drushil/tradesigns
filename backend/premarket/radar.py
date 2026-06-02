"""
Read-only pre-market radar and gap playbook.

This module surfaces pre-market moves and opening plans. It does not submit
orders and intentionally treats extended-hours execution as out of scope.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Optional

import pandas as pd

from backend.runtime.env import _env_bool, _env_float, _env_int, _env_value

try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    NY_TZ = None


@dataclass
class GapFeatures:
    ticker: str
    last_price: float
    prior_close: float
    gap_pct: float
    premarket_high: float
    premarket_low: float
    premarket_vwap: float
    premarket_volume: float
    premarket_rvol: Optional[float]
    spread_pct: Optional[float]
    news_score: float
    catalyst_label: str
    latest_headline: str
    earnings_context: dict
    data_quality: dict


def _now_ny(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(NY_TZ) if NY_TZ else now.astimezone(timezone.utc)


def session_window(now: Optional[datetime] = None) -> str:
    """Return the radar session name anchored in New York time."""
    ny_now = _now_ny(now)
    minutes = ny_now.hour * 60 + ny_now.minute
    if ny_now.weekday() == 6 and minutes >= 18 * 60:
        return "sunday_futures_watch"
    if ny_now.weekday() >= 5:
        return "closed"
    if 4 * 60 <= minutes < 8 * 60:
        return "early_premarket"
    if 8 * 60 <= minutes < 9 * 60 + 30:
        return "primary_premarket"
    if 9 * 60 + 30 <= minutes < 9 * 60 + 35:
        return "opening_confirmation"
    return "closed"


def _csv_tickers(raw: str) -> list[str]:
    seen = set()
    tickers = []
    for token in str(raw or "").split(","):
        ticker = token.strip().upper()
        if ticker and ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    return tickers


def radar_universe() -> list[str]:
    """Build the free-stack universe: configured tickers plus optional extras."""
    configured = _csv_tickers(_env_value("TICKER_UNIVERSE", "SPY,QQQ,GLD,TLT,AAPL"))
    extras = _csv_tickers(_env_value("PREMARKET_EXTRA_TICKERS", ""))

    advisory = []
    try:
        from backend.advisory import ADVISORY_UNIVERSE

        advisory = [
            str(item.get("data_symbol") or "").upper()
            for item in ADVISORY_UNIVERSE.get("US", [])
            if item.get("trade_target", True) and item.get("data_symbol")
        ]
    except Exception:
        advisory = []

    return _csv_tickers(",".join(configured + advisory + extras))


def _fetch_extended_bars(ticker: str):
    try:
        import yfinance as yf

        return yf.download(
            ticker,
            period="5d",
            interval="1m",
            prepost=True,
            progress=False,
            auto_adjust=True,
        )
    except Exception:
        return None


def _normalise_bars(bars) -> Optional[pd.DataFrame]:
    if bars is None or getattr(bars, "empty", True):
        return None
    df = bars.copy()
    if isinstance(df.columns, pd.MultiIndex):
        try:
            df.columns = df.columns.get_level_values(0)
        except Exception:
            return None
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True, errors="coerce")
    if df.index.tz is None:
        df.index = df.index.tz_localize(timezone.utc)
    df.index = df.index.tz_convert(NY_TZ) if NY_TZ else df.index.tz_convert(timezone.utc)
    return df.dropna(subset=["Close"]) if "Close" in df else None


def _session_slices(df: pd.DataFrame, now: Optional[datetime] = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    ny_now = _now_ny(now)
    today = ny_now.date()
    idx_dates = df.index.date
    today_bars = df[idx_dates == today]
    pre = today_bars[
        (today_bars.index.time >= time(4, 0))
        & (today_bars.index.time < time(9, 30))
    ]
    regular_prior = df[
        (df.index.date < today)
        & (df.index.time >= time(9, 30))
        & (df.index.time <= time(16, 0))
    ]
    return pre, regular_prior


def _previous_premarket_volumes(df: pd.DataFrame, today) -> list[float]:
    volumes = []
    for day in sorted({d for d in df.index.date if d < today})[-4:]:
        day_pre = df[
            (df.index.date == day)
            & (df.index.time >= time(4, 0))
            & (df.index.time < time(9, 30))
        ]
        if not day_pre.empty and "Volume" in day_pre:
            volumes.append(float(day_pre["Volume"].fillna(0).sum()))
    return volumes


def _latest_quote_spread_pct(ticker: str) -> Optional[float]:
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest

        api_key = os.getenv("ALPACA_API_KEY")
        secret = os.getenv("ALPACA_SECRET_KEY")
        if not api_key or not secret:
            return None
        client = StockHistoricalDataClient(api_key, secret)
        quote = client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=ticker))[ticker]
        bid = float(getattr(quote, "bid_price", 0) or 0)
        ask = float(getattr(quote, "ask_price", 0) or 0)
        mid = (bid + ask) / 2 if bid and ask else 0
        if mid <= 0 or ask < bid:
            return None
        return round((ask - bid) / mid * 100, 4)
    except Exception:
        return None


def _catalyst_label(news_score: float, news_meta: dict, earnings_context: dict) -> str:
    if earnings_context.get("blocked") or earnings_context.get("filing_date"):
        return "earnings"
    headline = str(news_meta.get("latest_headline") or "").lower()
    if any(word in headline for word in ("upgrade", "downgrade", "analyst", "price target")):
        return "analyst"
    if any(word in headline for word in ("merger", "acquire", "acquisition", "takeover")):
        return "m_and_a"
    if any(word in headline for word in ("guidance", "revenue", "profit", "earnings")):
        return "company_news"
    if abs(float(news_score or 0)) >= 0.4:
        return "news_sentiment"
    return "unknown"


def build_gap_features(ticker: str, bars=None, now: Optional[datetime] = None,
                       earnings_context: Optional[dict] = None) -> Optional[GapFeatures]:
    df = _normalise_bars(bars if bars is not None else _fetch_extended_bars(ticker))
    if df is None or df.empty:
        return None

    pre, regular_prior = _session_slices(df, now)
    if pre.empty or regular_prior.empty:
        return None

    prior_close = float(regular_prior["Close"].dropna().iloc[-1])
    close = pre["Close"].dropna()
    volume = pre["Volume"].fillna(0) if "Volume" in pre else pd.Series([], dtype=float)
    if prior_close <= 0 or close.empty:
        return None

    last_price = float(close.iloc[-1])
    typical = (pre["High"].fillna(close) + pre["Low"].fillna(close) + pre["Close"].fillna(close)) / 3
    vol_sum = float(volume.sum()) if len(volume) else 0.0
    vwap = float((typical * volume).sum() / vol_sum) if vol_sum > 0 else last_price
    today = _now_ny(now).date()
    previous_volumes = _previous_premarket_volumes(df, today)
    avg_previous_volume = sum(previous_volumes) / len(previous_volumes) if previous_volumes else 0.0
    rvol = round(vol_sum / avg_previous_volume, 3) if avg_previous_volume > 0 else None

    try:
        from backend.signals.engine import news_sentiment_score

        news_score, news_meta = news_sentiment_score(ticker, pre_news_composite=0.2)
    except Exception:
        news_score, news_meta = 0.0, {}

    earnings_context = earnings_context or {}
    label = _catalyst_label(news_score, news_meta, earnings_context)

    return GapFeatures(
        ticker=ticker.upper(),
        last_price=round(last_price, 4),
        prior_close=round(prior_close, 4),
        gap_pct=round((last_price - prior_close) / prior_close * 100, 4),
        premarket_high=round(float(pre["High"].dropna().max()), 4),
        premarket_low=round(float(pre["Low"].dropna().min()), 4),
        premarket_vwap=round(vwap, 4),
        premarket_volume=round(vol_sum, 2),
        premarket_rvol=rvol,
        spread_pct=_latest_quote_spread_pct(ticker),
        news_score=round(float(news_score or 0), 4),
        catalyst_label=label,
        latest_headline=str(news_meta.get("latest_headline") or "")[:160],
        earnings_context=earnings_context,
        data_quality={
            "source": "yfinance_prepost",
            "premarket_rows": int(len(pre)),
            "prior_regular_rows": int(len(regular_prior)),
            "previous_premarket_volume_samples": int(len(previous_volumes)),
        },
    )


def classify_gap(features: GapFeatures) -> dict:
    """Classify a pre-market gap into an opening playbook bucket."""
    gap_abs = abs(features.gap_pct)
    spread_max = _env_float("PREMARKET_MAX_SPREAD_PCT", 0.75)
    min_gap = _env_float("PREMARKET_MIN_GAP_PCT", 1.0)
    strong_gap = _env_float("PREMARKET_STRONG_GAP_PCT", 2.0)
    min_rvol = _env_float("PREMARKET_MIN_RVOL", 1.5)
    min_volume = _env_float("PREMARKET_MIN_VOLUME", 50000.0)

    direction = "up" if features.gap_pct > 0 else ("down" if features.gap_pct < 0 else "flat")
    reasons = []

    if features.spread_pct is not None and features.spread_pct > spread_max:
        return {
            "classification": "ignore_wide_spread",
            "direction": direction,
            "score": 0.0,
            "reasons": [f"spread {features.spread_pct:.2f}% > {spread_max:.2f}%"],
            "opening_plan": "Ignore unless spread normalizes after the open.",
        }

    score = 0.0
    if gap_abs >= min_gap:
        score += min(gap_abs / strong_gap, 2.0)
        reasons.append(f"gap {features.gap_pct:+.2f}%")
    if features.premarket_volume >= min_volume:
        score += 0.7
        reasons.append(f"pre-market volume {features.premarket_volume:,.0f}")
    if features.premarket_rvol is not None and features.premarket_rvol >= min_rvol:
        score += 0.8
        reasons.append(f"RVOL {features.premarket_rvol:.2f}x")
    if features.catalyst_label != "unknown":
        score += 0.7
        reasons.append(f"catalyst {features.catalyst_label}")
    if abs(features.news_score) >= 0.35:
        score += 0.3
        reasons.append(f"news score {features.news_score:+.2f}")

    if gap_abs < min_gap:
        bucket = "no_action_small_gap"
        plan = "No pre-market action; monitor regular signal cycle only."
    elif score >= 3.0 and features.catalyst_label != "unknown":
        bucket = "gap_continuation_watch"
        plan = "Watch PMH/PML and pre-market VWAP; require 1-5 min ORB confirmation."
    elif score >= 2.0:
        bucket = "opening_range_watch"
        plan = "Track PMH/PML; wait for first 1-5 min range break before any trade alert."
    elif features.catalyst_label == "unknown":
        bucket = "gap_fade_or_ignore"
        plan = "Treat as fragile until volume/catalyst confirms; avoid chasing the open."
    else:
        bucket = "catalyst_watch"
        plan = "Catalyst exists, but liquidity/volume confirmation is still required."

    return {
        "classification": bucket,
        "direction": direction,
        "score": round(score, 3),
        "reasons": reasons,
        "opening_plan": plan,
    }


def _format_radar_message(rows: list[dict], window: str, now: datetime) -> str:
    ny = _now_ny(now)
    if not rows:
        return f"Pre-market radar - {window} - {ny:%Y-%m-%d %H:%M ET}\nNo meaningful gaps found."

    lines = [f"Pre-market radar - {window} - {ny:%Y-%m-%d %H:%M ET}"]
    for row in rows[:_env_int("PREMARKET_DISCORD_MAX_ROWS", 8)]:
        features = row["features"]
        playbook = row["playbook"]
        rvol = features.get("premarket_rvol")
        rvol_text = f", RVOL {rvol:.2f}x" if rvol is not None else ""
        spread = features.get("spread_pct")
        spread_text = f", spread {spread:.2f}%" if spread is not None else ""
        lines.append(
            f"{features['ticker']}: {features['gap_pct']:+.2f}% "
            f"({playbook['classification']}, score {playbook['score']:.1f}"
            f"{rvol_text}{spread_text})"
        )
        lines.append(f"  Plan: {playbook['opening_plan']}")
        if features.get("latest_headline"):
            lines.append(f"  Catalyst: {features['latest_headline']}")
    return "\n".join(lines)[:1900]


def _send_discord(text: str) -> bool:
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not webhook:
        return False
    try:
        import requests

        return requests.post(webhook, json={"content": text}, timeout=10).ok
    except Exception:
        return False


def run_premarket_radar(now: Optional[datetime] = None, force: bool = False) -> dict:
    """Run one read-only radar cycle and persist the ranked snapshot."""
    now = now or datetime.now(timezone.utc)
    window = session_window(now)
    if window == "closed" and not force:
        return {"ran": False, "reason": "outside_premarket_window", "window": window}

    tickers = radar_universe()[:_env_int("PREMARKET_MAX_TICKERS", 40)]

    # Batch-prefetch NewsAPI once per cycle (1 call per ~12 tickers) so the
    # per-ticker news_sentiment_score reads inside build_gap_features hit the
    # DB cache instead of triggering N individual NewsAPI requests. Critical
    # on the first morning fire when the cache is cold — without this, a
    # 40-ticker radar run can burn ~40 of the 100/day free-tier NewsAPI quota.
    try:
        from backend.signals.engine import prefetch_newsapi_batch

        prefetch_newsapi_batch(tickers)
    except Exception:
        pass

    try:
        from backend.earnings.scanner import scan_earnings_guard

        earnings = scan_earnings_guard(tickers)
    except Exception:
        earnings = {}

    rows = []
    for ticker in tickers:
        try:
            features = build_gap_features(ticker, now=now, earnings_context=earnings.get(ticker, {}))
            if not features:
                continue
            playbook = classify_gap(features)
            if playbook["classification"] == "no_action_small_gap":
                continue
            rows.append({
                "features": features.__dict__,
                "playbook": playbook,
            })
        except Exception:
            continue

    rows.sort(key=lambda row: (row["playbook"]["score"], abs(row["features"]["gap_pct"])), reverse=True)
    cycle = {
        "cycle_started_at": now.astimezone(timezone.utc).isoformat(),
        "session_window": window,
        "tickers_scanned": len(tickers),
        "candidates": rows,
    }

    try:
        from database.client import log_event, upsert_premarket_radar_snapshots

        upsert_premarket_radar_snapshots(cycle)
        log_event("INFO", "premarket_radar_complete", {
            "session_window": window,
            "tickers_scanned": len(tickers),
            "candidates": len(rows),
        })
    except Exception:
        pass

    if _env_bool("PREMARKET_RADAR_DISCORD_ENABLED", True):
        _send_discord(_format_radar_message(rows, window, now))

    return {**cycle, "ran": True, "candidate_count": len(rows)}

