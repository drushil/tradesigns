"""
backend/signals/engine.py
Computes all micro-signals per ticker using free data sources.
Returns a normalised composite score -1.0 to +1.0.
"""
import os
import time
import logging
import requests
import numpy as np
import pandas as pd
import yfinance as yf
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")

# yfinance prints its own error messages to stderr even when exceptions are caught
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clamp(val: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, val))


def _get_bars(ticker: str, period: str = "5d", interval: str = "1m") -> Optional[pd.DataFrame]:
    try:
        df = yf.download(ticker, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 20:
            return None
        return df
    except Exception:
        return None


# ── Signal 1: RSI Divergence (free — yfinance) ────────────────────────────────

def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs   = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def rsi_divergence_score(ticker: str) -> tuple[float, dict]:
    """
    Bullish divergence: price makes lower low, RSI makes higher low → +signal
    Bearish divergence: price makes higher high, RSI makes lower high → -signal
    Returns score -1 to +1 and metadata dict.
    """
    df = _get_bars(ticker, period="5d", interval="1m")
    if df is None:
        return 0.0, {"error": "no_data"}

    close  = df["Close"].squeeze()
    rsi    = compute_rsi(close, 14)
    latest_rsi   = float(rsi.iloc[-1])
    latest_price = float(close.iloc[-1])

    # Look at last 30 bars for divergence
    window = 30
    price_w = close.iloc[-window:]
    rsi_w   = rsi.iloc[-window:]

    # Oversold/overbought base signal
    if latest_rsi < 30:
        base_score = 0.6    # oversold — bullish lean
    elif latest_rsi > 70:
        base_score = -0.6   # overbought — bearish lean
    else:
        base_score = (50 - latest_rsi) / 50 * 0.4  # mild directional

    # Divergence check
    price_trend = float(price_w.iloc[-1]) - float(price_w.iloc[0])
    rsi_trend   = float(rsi_w.iloc[-1])  - float(rsi_w.iloc[0])

    divergence = 0.0
    if price_trend < 0 and rsi_trend > 0:
        divergence = 0.4    # bullish divergence
    elif price_trend > 0 and rsi_trend < 0:
        divergence = -0.4   # bearish divergence

    score = _clamp(base_score + divergence)
    return score, {
        "rsi": round(latest_rsi, 1),
        "price_trend": round(price_trend, 4),
        "rsi_trend": round(rsi_trend, 2),
        "divergence": divergence != 0,
    }


# ── Signal 2: VWAP Deviation (free — yfinance) ───────────────────────────────

def vwap_deviation_score(ticker: str) -> tuple[float, dict]:
    """
    Deviation from intraday VWAP.
    Price below VWAP → potential mean-reversion long (+)
    Price above VWAP by excess → potential short / overbought (-)
    """
    df = _get_bars(ticker, period="1d", interval="1m")
    if df is None:
        return 0.0, {"error": "no_data"}

    high  = df["High"].squeeze()
    low   = df["Low"].squeeze()
    close = df["Close"].squeeze()
    vol   = df["Volume"].squeeze()

    typical_price = (high + low + close) / 3
    vwap = (typical_price * vol).cumsum() / vol.cumsum()

    latest_price = float(close.iloc[-1])
    latest_vwap  = float(vwap.iloc[-1])

    pct_dev = (latest_price - latest_vwap) / latest_vwap * 100

    # Scores: deviation from VWAP drives mean-reversion signal
    if pct_dev < -0.3:
        score = min(0.8, abs(pct_dev) * 1.5)   # below VWAP → bullish
    elif pct_dev > 0.3:
        score = max(-0.8, -pct_dev * 1.5)       # above VWAP → bearish
    else:
        score = -pct_dev / 0.3 * 0.3            # within band → mild

    return _clamp(score), {
        "vwap": round(latest_vwap, 4),
        "price": round(latest_price, 4),
        "pct_deviation": round(pct_dev, 3),
    }


# ── Signal 3: News Sentiment (yfinance + Finviz — free, no key) ──────────────

_news_cache: dict = {}
_NEWS_CACHE_TTL = 900  # 15 minutes
_NEWS_STALE_MAX_AGE = 12 * 60  # minutes
_NEWS_FORCE_REFRESH_COMPOSITE = float(os.getenv("NEWS_FORCE_REFRESH_COMPOSITE", "0.18"))
_NEWS_FORCE_REFRESH_SIGNAL = float(os.getenv("NEWS_FORCE_REFRESH_SIGNAL", "0.55"))

_FINVIZ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


def _fetch_newsapi_headlines(ticker: str) -> list:
    """
    NewsAPI fallback — only called when yfinance returns nothing.
    Free tier: 100 req/day. With 15-min cache this stays well within limit
    as long as yfinance is healthy (NewsAPI only fires on yfinance failures).
    Requires NEWSAPI_KEY env var; silently skipped if absent.
    """
    if not NEWSAPI_KEY:
        return []
    resp = requests.get(
        "https://newsapi.org/v2/everything",
        params={"q": ticker, "language": "en", "sortBy": "publishedAt",
                "pageSize": 10, "apiKey": NEWSAPI_KEY},
        timeout=5,
    )
    resp.raise_for_status()
    try:
        from database.client import record_newsapi_usage
        record_newsapi_usage(ticker)
    except Exception:
        pass
    articles = resp.json().get("articles", [])
    return [(a.get("title") or "").strip() for a in articles[:10] if a.get("title")]


def _fetch_finviz_headlines(ticker: str) -> list:
    """Scrape recent headlines from finviz.com/quote.ashx — no API key needed."""
    url = f"https://finviz.com/quote.ashx?t={ticker}"
    resp = requests.get(url, headers=_FINVIZ_HEADERS, timeout=5)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find(id="news-table")
    if not table:
        return []
    return [row.a.text.strip() for row in table.find_all("tr") if row.a][:10]


def _fetch_stocktwits(ticker: str) -> tuple[list, int, int]:
    """
    Fetch recent StockTwits messages for a ticker.
    Returns (body_texts, explicit_bullish_count, explicit_bearish_count).
    Explicit user-tagged sentiment is a stronger signal than keyword matching.
    """
    url = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
    resp = requests.get(url, timeout=5)
    resp.raise_for_status()
    messages = resp.json().get("messages", [])

    bodies, bullish, bearish = [], 0, 0
    for m in messages[:20]:
        body = (m.get("body") or "").strip()
        if body:
            bodies.append(body[:120])
        tag = ((m.get("entities") or {}).get("sentiment") or {}).get("basic", "")
        if tag == "Bullish":
            bullish += 1
        elif tag == "Bearish":
            bearish += 1
    return bodies, bullish, bearish


def _cached_news_result(ticker: str, max_age_minutes: int, force_refresh: bool) -> Optional[tuple[float, dict]]:
    if force_refresh:
        return None
    try:
        from database.client import get_news_cache
        row = get_news_cache(ticker, max_age_minutes=max_age_minutes)
    except Exception:
        row = None
    if not row:
        return None

    meta = dict(row.get("meta_json") or {})
    meta.update({
        "cache_hit": "supabase",
        "cache_age_minutes": _cache_age_minutes(row.get("fetched_at")),
        "stale_news": False,
    })
    return float(row.get("sentiment_score") or 0.0), meta


def _stale_news_result(ticker: str) -> Optional[tuple[float, dict]]:
    try:
        from database.client import get_news_cache
        row = get_news_cache(ticker, max_age_minutes=_NEWS_STALE_MAX_AGE)
    except Exception:
        row = None
    if not row:
        return None

    meta = dict(row.get("meta_json") or {})
    meta.update({
        "cache_hit": "supabase_stale",
        "cache_age_minutes": _cache_age_minutes(row.get("fetched_at")),
        "stale_news": True,
    })
    return float(row.get("sentiment_score") or 0.0), meta


def _cache_age_minutes(value) -> Optional[int]:
    if not value:
        return None
    try:
        fetched = datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
        return max(0, int((datetime.utcnow() - fetched).total_seconds() // 60))
    except ValueError:
        return None


def _save_news_result(ticker: str, score: float, meta: dict, headlines: list):
    try:
        from database.client import upsert_news_cache
        upsert_news_cache(ticker, score, meta, headlines)
    except Exception:
        pass


def news_sentiment_score(
    ticker: str,
    force_refresh: bool = False,
    cache_ttl_seconds: int = _NEWS_CACHE_TTL,
) -> tuple[float, dict]:
    """
    Combines yfinance + Finviz headlines + StockTwits messages (all free, no key).
    StockTwits explicit Bullish/Bearish tags are counted as direct signal;
    all text sources are also keyword-scored.
    Returns score -1 to +1.
    """
    cache_key = ticker.upper()
    now = time.time()
    if not force_refresh and cache_key in _news_cache:
        cached_time, cached_result = _news_cache[cache_key]
        if now - cached_time < cache_ttl_seconds:
            return cached_result

    cached = _cached_news_result(
        cache_key,
        max_age_minutes=max(1, int(cache_ttl_seconds / 60)),
        force_refresh=force_refresh,
    )
    if cached:
        _news_cache[cache_key] = (now, cached)
        return cached

    positive_kw = ["surge", "soar", "rally", "beat", "record", "upgrade",
                   "bullish", "growth", "profit", "revenue", "strong",
                   "gains", "outperform", "buy", "positive"]
    negative_kw = ["plunge", "crash", "fall", "miss", "downgrade", "bearish",
                   "loss", "decline", "sell", "weak", "concern", "risk",
                   "drop", "underperform", "negative", "warning"]

    texts = []
    sources = []
    st_bullish = st_bearish = 0

    # Source 1: yfinance; Source 1b: NewsAPI backup when yfinance returns nothing
    try:
        for a in (yf.Ticker(ticker).news or [])[:5]:
            title = (a.get("title") or "").strip()
            if title:
                texts.append(title)
        if texts:
            sources.append("yfinance")
    except Exception:
        pass
    if not texts:
        try:
            newsapi_texts = _fetch_newsapi_headlines(ticker)
            texts.extend(newsapi_texts)
            if newsapi_texts:
                sources.append("newsapi")
        except Exception:
            pass

    # Source 2: Finviz
    try:
        finviz_texts = _fetch_finviz_headlines(ticker)
        texts.extend(finviz_texts)
        if finviz_texts:
            sources.append("finviz")
    except Exception:
        pass

    # Source 3: StockTwits (bodies for keyword scoring + explicit tags)
    try:
        bodies, st_bullish, st_bearish = _fetch_stocktwits(ticker)
        texts.extend(bodies)
        if bodies or st_bullish or st_bearish:
            sources.append("stocktwits")
    except Exception:
        pass

    # Deduplicate while preserving order
    seen, unique = set(), []
    for t in texts:
        key = t[:50].lower()
        if key not in seen:
            seen.add(key)
            unique.append(t)

    # Keyword scoring across all text sources
    pos_count = neg_count = 0
    for text in unique:
        low = text.lower()
        pos_count += sum(1 for kw in positive_kw if kw in low)
        neg_count += sum(1 for kw in negative_kw if kw in low)

    # StockTwits explicit tags count as 2 keyword hits each (direct user intent)
    pos_count += st_bullish * 2
    neg_count += st_bearish * 2

    total = pos_count + neg_count
    score = _clamp((pos_count - neg_count) / total) if total else 0.0

    if not unique:
        stale = _stale_news_result(cache_key)
        if stale:
            _news_cache[cache_key] = (now, stale)
            return stale

    meta = {
        "articles_found":   len(unique),
        "positive_hits":    pos_count,
        "negative_hits":    neg_count,
        "st_bullish":       st_bullish,
        "st_bearish":       st_bearish,
        "sources":          sources,
        "latest_headline":  unique[0][:80] if unique else "",
        "cache_hit":        False,
        "stale_news":       False,
        "force_refreshed":  force_refresh,
    }

    _news_cache[cache_key] = (now, (score, meta))
    if unique:
        _save_news_result(cache_key, score, meta, unique[:25])
    return score, meta


# ── Signal 4: Tape Aggression (momentum proxy via yfinance volume) ────────────

def tape_aggression_score(ticker: str) -> tuple[float, dict]:
    """
    Approximation of tape aggression using volume spike + price momentum.
    Real tape data requires paid feed; this is the free proxy.
    Measures: volume vs 20-bar average, and direction of recent candles.
    """
    df = _get_bars(ticker, period="2d", interval="5m")
    if df is None:
        return 0.0, {"error": "no_data"}

    close  = df["Close"].squeeze()
    volume = df["Volume"].squeeze()

    # Volume spike ratio vs 20-bar mean
    vol_mean  = float(volume.rolling(20).mean().iloc[-1])
    vol_now   = float(volume.iloc[-1])
    vol_ratio = vol_now / vol_mean if vol_mean > 0 else 1.0

    # Price momentum: last 3 bars direction
    recent_returns = close.pct_change().iloc[-4:]
    momentum = float(recent_returns.sum())

    # Aggression = volume spike × momentum direction
    spike_signal = min((vol_ratio - 1.0) / 2.0, 1.0)  # 0 to 1
    direction    = 1 if momentum > 0 else -1

    score = _clamp(spike_signal * direction * 0.8)
    return score, {
        "volume_ratio": round(vol_ratio, 2),
        "momentum_3bar": round(momentum * 100, 3),
        "direction": "bullish" if direction > 0 else "bearish",
    }


# ── Signal 5: Order Book Imbalance (Alpaca free — best bid/ask spread proxy) ──

def order_book_score(ticker: str) -> tuple[float, dict]:
    """
    Uses Alpaca paper account to get latest quote (bid/ask).
    Computes spread and side pressure as a proxy for order book imbalance.
    Free via Alpaca paper account websocket/REST.
    """
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest

        api_key    = os.getenv("ALPACA_API_KEY")
        secret_key = os.getenv("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            return 0.0, {"error": "no_alpaca_keys"}

        client = StockHistoricalDataClient(api_key, secret_key)
        req    = StockLatestQuoteRequest(symbol_or_symbols=ticker)
        quote  = client.get_stock_latest_quote(req)[ticker]

        bid = float(quote.bid_price)
        ask = float(quote.ask_price)
        mid = (bid + ask) / 2

        spread_pct = (ask - bid) / mid * 100 if mid > 0 else 0

        # Bid/ask size imbalance (if available)
        bid_size = float(quote.bid_size or 1)
        ask_size = float(quote.ask_size or 1)
        total    = bid_size + ask_size
        imbalance = (bid_size - ask_size) / total if total > 0 else 0.0

        # Tight spread = liquid = higher signal confidence
        spread_factor = max(0.3, 1.0 - spread_pct * 5)
        score = _clamp(imbalance * spread_factor)

        return score, {
            "bid": bid, "ask": ask,
            "spread_pct": round(spread_pct, 4),
            "bid_size": bid_size, "ask_size": ask_size,
            "imbalance": round(imbalance, 3),
        }
    except Exception as e:
        return 0.0, {"error": str(e)[:80]}


# ── Signal 6: MACD Crossover (free — yfinance) ───────────────────────────────

def macd_crossover_score(ticker: str) -> tuple[float, dict]:
    """
    MACD on 5-minute bars.
    Positive histogram/cross-up implies bullish momentum; negative implies bearish.
    """
    df = _get_bars(ticker, period="5d", interval="5m")
    if df is None:
        return 0.0, {"error": "no_data"}

    close = df["Close"].squeeze()
    ema_fast = close.ewm(span=12, adjust=False).mean()
    ema_slow = close.ewm(span=26, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal

    latest_hist = float(hist.iloc[-1])
    prev_hist = float(hist.iloc[-2])
    latest_price = float(close.iloc[-1])
    hist_pct = latest_hist / latest_price * 100 if latest_price else 0.0
    hist_slope = latest_hist - prev_hist

    crossed_up = prev_hist <= 0 < latest_hist
    crossed_down = prev_hist >= 0 > latest_hist

    base = _clamp(hist_pct / 0.15)
    if crossed_up:
        base += 0.35
    elif crossed_down:
        base -= 0.35

    return _clamp(base), {
        "macd": round(float(macd.iloc[-1]), 5),
        "signal": round(float(signal.iloc[-1]), 5),
        "histogram": round(latest_hist, 5),
        "histogram_pct": round(hist_pct, 4),
        "histogram_slope": round(hist_slope, 5),
        "crossed_up": crossed_up,
        "crossed_down": crossed_down,
    }


# ── Signal 7: Relative Strength vs SPY (free — yfinance) ─────────────────────

def relative_strength_score(ticker: str, benchmark: str = "SPY") -> tuple[float, dict]:
    """
    Compares ticker performance to SPY over 5/10/20 recent 5-minute bars.
    Outperformance is bullish; underperformance is bearish.
    """
    if ticker.upper() == benchmark.upper():
        return 0.0, {
            "benchmark": benchmark,
            "rs_5bar": 0.0,
            "rs_10bar": 0.0,
            "rs_20bar": 0.0,
            "ticker_ret_10bar": 0.0,
            "spy_ret_10bar": 0.0,
            "note": "ticker_is_benchmark",
        }

    ticker_df = _get_bars(ticker, period="5d", interval="5m")
    bench_df = _get_bars(benchmark, period="5d", interval="5m")
    if ticker_df is None or bench_df is None:
        return 0.0, {"error": "no_data"}

    ticker_close = ticker_df["Close"].squeeze()
    bench_close = bench_df["Close"].squeeze()

    def ret_pct(series: pd.Series, bars: int) -> float:
        if len(series) <= bars:
            return 0.0
        start = float(series.iloc[-bars - 1])
        end = float(series.iloc[-1])
        return (end - start) / start * 100 if start else 0.0

    rs_values = {}
    weighted_rs = 0.0
    weights = {5: 0.25, 10: 0.45, 20: 0.30}
    for bars, weight in weights.items():
        ticker_ret = ret_pct(ticker_close, bars)
        bench_ret = ret_pct(bench_close, bars)
        rel = ticker_ret - bench_ret
        rs_values[bars] = (ticker_ret, bench_ret, rel)
        weighted_rs += rel * weight

    score = _clamp(weighted_rs / 1.5)
    return score, {
        "benchmark": benchmark,
        "rs_5bar": round(rs_values[5][2], 3),
        "rs_10bar": round(rs_values[10][2], 3),
        "rs_20bar": round(rs_values[20][2], 3),
        "ticker_ret_10bar": round(rs_values[10][0], 3),
        "spy_ret_10bar": round(rs_values[10][1], 3),
        "weighted_rs": round(weighted_rs, 3),
    }


# ── Signal 8: Earnings Proximity Multiplier (free — yfinance) ────────────────

_earnings_cache: dict = {}
_EARNINGS_CACHE_TTL = 21600  # 6 hours


def earnings_proximity_signal(ticker: str) -> tuple[float, dict]:
    """
    Finds next earnings date when available.
    Returns a neutral score and a multiplier used to amplify the final composite.
    """
    cache_key = ticker.upper()
    now_ts = time.time()
    if cache_key in _earnings_cache:
        cached_time, cached_result = _earnings_cache[cache_key]
        if now_ts - cached_time < _EARNINGS_CACHE_TTL:
            return cached_result

    result = (0.0, {
        "days_to_earnings": None,
        "earnings_multiplier": 1.0,
        "source": "yfinance",
    })

    try:
        ticker_obj = yf.Ticker(ticker)
        dates = ticker_obj.get_earnings_dates(limit=8)
        if dates is not None and not dates.empty:
            now = pd.Timestamp.utcnow()
            if dates.index.tz is None:
                idx = dates.index.tz_localize("UTC")
            else:
                idx = dates.index.tz_convert("UTC")
            future = idx[idx >= now]
            if len(future) > 0:
                next_date = future[0]
                days = max(0, int((next_date - now).total_seconds() // 86400))
                if days <= 1:
                    mult = 1.5
                elif days <= 3:
                    mult = 1.35
                elif days <= 7:
                    mult = 1.15
                else:
                    mult = 1.0
                result = (0.0, {
                    "days_to_earnings": days,
                    "earnings_date": next_date.date().isoformat(),
                    "earnings_multiplier": mult,
                    "source": "yfinance",
                })
    except Exception as e:
        result = (0.0, {
            "days_to_earnings": None,
            "earnings_multiplier": 1.0,
            "source": "yfinance",
            "error": str(e)[:80],
        })

    _earnings_cache[cache_key] = (now_ts, result)
    return result


# ── ATR (Average True Range) ──────────────────────────────────────────────────

def compute_atr(ticker: str, period: int = 14) -> dict:
    """
    Computes ATR on 5-min bars.
    Returns atr_pct and suggested_stop_pct (1.5× ATR, clamped 0.5%–4%).
    """
    try:
        df = _get_bars(ticker, period="5d", interval="5m")
        if df is None or len(df) < period + 1:
            return {"atr_pct": None, "suggested_stop_pct": None}

        high  = df["High"].squeeze()
        low   = df["Low"].squeeze()
        close = df["Close"].squeeze()
        prev_close = close.shift(1)

        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)

        atr = float(tr.rolling(period).mean().iloc[-1])
        current_price = float(close.iloc[-1])

        if current_price <= 0 or pd.isna(atr):
            return {"atr_pct": None, "suggested_stop_pct": None}

        atr_pct           = atr / current_price * 100
        suggested_stop_pct = max(0.5, min(4.0, 1.5 * atr_pct))

        return {
            "atr_pct":           round(atr_pct, 4),
            "suggested_stop_pct": round(suggested_stop_pct, 4),
            "atr_raw":           round(atr, 6),
        }
    except Exception:
        return {"atr_pct": None, "suggested_stop_pct": None}


# ── Signal 9: Bollinger Band Squeeze ─────────────────────────────────────────

_bollinger_cache: dict = {}
_BOLLINGER_CACHE_TTL = 300  # 5 minutes


def bollinger_squeeze_score(ticker: str) -> tuple[float, dict]:
    """
    20-bar Bollinger Bands on 5-min bars, 2 std devs.
    Squeeze = band width < 50% of 50-bar average.
    +0.7 breakout above, -0.7 below, +0.3 squeeze only, 0.0 no squeeze.
    """
    cache_key = ticker
    now = time.time()
    if cache_key in _bollinger_cache:
        cached_time, cached_result = _bollinger_cache[cache_key]
        if now - cached_time < _BOLLINGER_CACHE_TTL:
            return cached_result

    try:
        df = _get_bars(ticker, period="5d", interval="5m")
        if df is None or len(df) < 55:
            result = (0.0, {"error": "insufficient_data"})
            _bollinger_cache[cache_key] = (now, result)
            return result

        close = df["Close"].squeeze()
        rolling_mean = close.rolling(20).mean()
        rolling_std  = close.rolling(20).std()
        upper_band   = rolling_mean + 2 * rolling_std
        lower_band   = rolling_mean - 2 * rolling_std
        band_width   = (upper_band - lower_band) / rolling_mean.replace(0, np.nan)
        avg_bw       = band_width.rolling(50).mean()

        latest_price  = float(close.iloc[-1])
        latest_upper  = float(upper_band.iloc[-1])
        latest_lower  = float(lower_band.iloc[-1])
        latest_bw     = float(band_width.iloc[-1])
        avg_bw_val    = float(avg_bw.iloc[-1])

        if pd.isna(avg_bw_val) or avg_bw_val <= 0 or pd.isna(latest_bw):
            result = (0.0, {"error": "insufficient_avg_data"})
            _bollinger_cache[cache_key] = (now, result)
            return result

        squeeze = latest_bw < 0.5 * avg_bw_val

        if squeeze:
            if latest_price > latest_upper:
                score = 0.7
            elif latest_price < latest_lower:
                score = -0.7
            else:
                score = 0.3
        else:
            score = 0.0

        result = (score, {
            "squeeze":        squeeze,
            "band_width":     round(latest_bw, 4),
            "avg_band_width": round(avg_bw_val, 4),
            "bw_ratio":       round(latest_bw / avg_bw_val, 3),
            "upper_band":     round(latest_upper, 4),
            "lower_band":     round(latest_lower, 4),
            "price":          round(latest_price, 4),
        })
    except Exception as e:
        result = (0.0, {"error": str(e)[:80]})

    _bollinger_cache[cache_key] = (now, result)
    return result


# ── Signal 10: Put/Call Ratio ─────────────────────────────────────────────────

_pcr_cache: dict = {}
_PCR_CACHE_TTL = 1800  # 30 minutes


def put_call_ratio_score(ticker: str) -> tuple[float, dict]:
    """
    Put/call ratio from yfinance nearest expiry options.
    >1.5 bearish (-0.5 to -0.8), <0.7 bullish (+0.4 to +0.7), neutral otherwise.
    Returns 0.0 if no options exist.
    """
    cache_key = ticker
    now = time.time()
    if cache_key in _pcr_cache:
        cached_time, cached_result = _pcr_cache[cache_key]
        if now - cached_time < _PCR_CACHE_TTL:
            return cached_result

    try:
        ticker_obj = yf.Ticker(ticker)
        expiries   = ticker_obj.options
        if not expiries:
            result = (0.0, {"error": "no_options"})
            _pcr_cache[cache_key] = (now, result)
            return result

        opt      = ticker_obj.option_chain(expiries[0])
        put_vol  = float(opt.puts["volume"].fillna(0).sum())
        call_vol = float(opt.calls["volume"].fillna(0).sum())

        if call_vol <= 0:
            result = (0.0, {"error": "zero_call_volume"})
            _pcr_cache[cache_key] = (now, result)
            return result

        pcr = put_vol / call_vol

        if pcr > 1.5:
            score = -(0.5 + min(0.3, (pcr - 1.5) / 1.0 * 0.3))
        elif pcr < 0.7:
            score = 0.4 + (0.7 - pcr) / 0.7 * 0.3
        else:
            score = 0.0

        result = (_clamp(score), {
            "pcr":         round(pcr, 3),
            "put_volume":  int(put_vol),
            "call_volume": int(call_vol),
            "expiry":      expiries[0],
        })
    except Exception as e:
        result = (0.0, {"error": str(e)[:80]})

    _pcr_cache[cache_key] = (now, result)
    return result


# ── Regime Detection ──────────────────────────────────────────────────────────

def detect_regime(ticker: str = "SPY") -> str:
    """
    Classifies current market regime:
    trending | ranging | high_vol | news_driven
    Uses VIX proxy via ^VIX and ADX approximation.
    """
    try:
        vix_df = yf.download("^VIX", period="2d", interval="1h",
                             progress=False, auto_adjust=True)
        vix = float(vix_df["Close"].iloc[-1]) if not vix_df.empty else 20.0
    except Exception:
        vix = 20.0

    if vix > 30:
        return "high_vol"

    df = _get_bars(ticker, period="5d", interval="15m")
    if df is None:
        return "ranging"

    close  = df["Close"].squeeze()
    # ADX approximation: std dev of returns / mean abs return
    returns   = close.pct_change().dropna()
    std_ret   = float(returns.rolling(14).std().iloc[-1])
    mean_ret  = float(returns.rolling(14).mean().iloc[-1])

    # Simple trend strength
    trend_score = abs(mean_ret) / (std_ret + 1e-9)

    if trend_score > 1.5:
        return "trending"
    else:
        return "ranging"


# ── Master signal composer ────────────────────────────────────────────────────

def compute_all_signals(ticker: str, weights: dict) -> dict:
    """
    Runs all 10 signals + ATR, applies profile weights, returns composite score
    plus full metadata for logging and display.
    """
    results = {}

    # Run all signals — failures return 0.0, never crash the cycle
    try:
        s1, m1 = rsi_divergence_score(ticker)
    except Exception:
        s1, m1 = 0.0, {"error": "signal_crashed"}

    try:
        s2, m2 = vwap_deviation_score(ticker)
    except Exception:
        s2, m2 = 0.0, {"error": "signal_crashed"}

    try:
        s4, m4 = tape_aggression_score(ticker)
    except Exception:
        s4, m4 = 0.0, {"error": "signal_crashed"}

    try:
        s5, m5 = order_book_score(ticker)
    except Exception:
        s5, m5 = 0.0, {"error": "signal_crashed"}

    try:
        s6, m6 = macd_crossover_score(ticker)
    except Exception:
        s6, m6 = 0.0, {"error": "signal_crashed"}

    try:
        s7, m7 = relative_strength_score(ticker)
    except Exception:
        s7, m7 = 0.0, {"error": "signal_crashed"}

    try:
        s8, m8 = earnings_proximity_signal(ticker)
    except Exception:
        s8, m8 = 0.0, {"earnings_multiplier": 1.0, "error": "signal_crashed"}

    try:
        s9, m9 = bollinger_squeeze_score(ticker)
    except Exception:
        s9, m9 = 0.0, {"error": "signal_crashed"}

    try:
        s10, m10 = put_call_ratio_score(ticker)
    except Exception:
        s10, m10 = 0.0, {"error": "signal_crashed"}

    atr_data = compute_atr(ticker)

    pre_news_signals = {
        "rsi_divergence":       s1,
        "vwap_deviation":       s2,
        "tape_aggression":      s4,
        "order_book_imbalance": s5,
        "macd_crossover":       s6,
        "relative_strength":    s7,
        "bollinger_squeeze":    s9,
        "put_call_ratio":       s10,
    }
    pre_news_weight_total = sum(max(0.0, weights.get(k, 0.0)) for k in pre_news_signals)
    if pre_news_weight_total <= 0:
        pre_news_weight_total = 1.0
    pre_news_composite = sum(
        score * (max(0.0, weights.get(name, 0.0)) / pre_news_weight_total)
        for name, score in pre_news_signals.items()
    )
    pre_news_composite *= m8.get("earnings_multiplier", 1.0)
    pre_news_composite = _clamp(pre_news_composite)

    force_news_refresh = (
        abs(pre_news_composite) >= _NEWS_FORCE_REFRESH_COMPOSITE
        or max(abs(s1), abs(s2), abs(s4), abs(s5)) >= _NEWS_FORCE_REFRESH_SIGNAL
    )

    try:
        s3, m3 = news_sentiment_score(ticker, force_refresh=force_news_refresh)
        m3["pre_news_composite"] = round(pre_news_composite, 4)
    except Exception:
        s3, m3 = 0.0, {"error": "signal_crashed"}

    results["rsi_divergence"]      = {"score": s1,  "meta": m1}
    results["vwap_deviation"]       = {"score": s2,  "meta": m2}
    results["news_sentiment"]       = {"score": s3,  "meta": m3}
    results["tape_aggression"]      = {"score": s4,  "meta": m4}
    results["order_book_imbalance"] = {"score": s5,  "meta": m5}
    results["macd_crossover"]       = {"score": s6,  "meta": m6}
    results["relative_strength"]    = {"score": s7,  "meta": m7}
    results["earnings_proximity"]   = {"score": s8,  "meta": m8}
    results["bollinger_squeeze"]    = {"score": s9,  "meta": m9}
    results["put_call_ratio"]       = {"score": s10, "meta": m10}

    # Weighted composite (earnings_proximity is a multiplier, not a weight)
    weighted_signals = {
        "rsi_divergence":      s1,
        "vwap_deviation":      s2,
        "news_sentiment":      s3,
        "tape_aggression":     s4,
        "order_book_imbalance": s5,
        "macd_crossover":      s6,
        "relative_strength":   s7,
        "bollinger_squeeze":   s9,
        "put_call_ratio":      s10,
    }
    weight_total = sum(max(0.0, weights.get(k, 0.0)) for k in weighted_signals)
    if weight_total <= 0:
        weight_total = 1.0
    composite = sum(
        score * (max(0.0, weights.get(name, 0.0)) / weight_total)
        for name, score in weighted_signals.items()
    )
    earnings_multiplier = m8.get("earnings_multiplier", 1.0)
    composite *= earnings_multiplier
    composite = _clamp(composite)

    regime = detect_regime()

    return {
        "ticker":              ticker,
        "composite_score":     round(composite, 4),
        "regime":              regime,
        "signals":             results,
        "weights_used":        weights,
        "earnings_multiplier": earnings_multiplier,
        "atr_data":            atr_data,
        "computed_at":         datetime.utcnow().isoformat(),
    }
