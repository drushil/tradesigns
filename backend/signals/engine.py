"""
backend/signals/engine.py
Computes all micro-signals per ticker using free data sources.
Returns a normalised composite score -1.0 to +1.0.
"""
import os
import time
import logging
import requests
import json
import numpy as np
import pandas as pd
import yfinance as yf
from bs4 import BeautifulSoup
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")

# yfinance prints its own error messages to stderr even when exceptions are caught
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


@dataclass
class RegimeState:
    market_regime: str
    intraday_regime: str
    vix: float
    sma200_price: float
    price_vs_sma200_pct: float
    trend_score: float = 0.0
    trend_threshold: float = 1.5
    trend_mean_return: float = 0.0
    trend_std_return: float = 0.0
    regime_reason: str = ""
    computed_at: str = ""
    yield_curve: float = 0.0          # T10Y2Y spread (10yr minus 2yr, %)
    yield_curve_state: str = "normal" # "inverted" | "flat" | "normal"

    def to_dict(self) -> dict:
        return asdict(self)

    def __str__(self) -> str:
        return self.intraday_regime


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clamp(val: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, val))


# ── Alpaca data client singleton ──────────────────────────────────────────────
_alpaca_data_client = None

def _get_alpaca_data_client():
    """Lazy-init singleton for Alpaca StockHistoricalDataClient."""
    global _alpaca_data_client
    if _alpaca_data_client is None:
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            api_key    = os.getenv("ALPACA_API_KEY")
            secret_key = os.getenv("ALPACA_SECRET_KEY")
            if api_key and secret_key:
                _alpaca_data_client = StockHistoricalDataClient(api_key, secret_key)
        except Exception:
            pass
    return _alpaca_data_client


# Per-cycle bar cache: (ticker, interval) → (timestamp, DataFrame)
_bars_cache: dict = {}
_BARS_CACHE_TTL = 90  # seconds — covers one full 10-min signal cycle


def _get_bars(ticker: str, period: str = "5d", interval: str = "1m") -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV bars. Tries Alpaca (real-time IEX) first; falls back to yfinance.
    Results are cached for 90 s to avoid duplicate fetches within one cycle.
    """
    cache_key = (ticker, interval)
    now_ts    = time.time()
    if cache_key in _bars_cache:
        cached_ts, cached_df = _bars_cache[cache_key]
        if now_ts - cached_ts < _BARS_CACHE_TTL:
            return cached_df

    df = _alpaca_bars(ticker, period, interval)
    if df is None:
        df = _yfinance_bars(ticker, period, interval)

    _bars_cache[cache_key] = (now_ts, df)
    return df


def _alpaca_bars(ticker: str, period: str, interval: str) -> Optional[pd.DataFrame]:
    """Fetch bars from Alpaca Data API (IEX, real-time on free tier)."""
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        client = _get_alpaca_data_client()
        if client is None:
            return None

        # Map yfinance period/interval → Alpaca timeframe + window
        _tf_map = {
            "1m":  TimeFrame.Minute,
            "5m":  TimeFrame(5,  TimeFrameUnit.Minute),
            "15m": TimeFrame(15, TimeFrameUnit.Minute),
            "1h":  TimeFrame.Hour,
            "1d":  TimeFrame.Day,
        }
        _days_map = {"1d": 2, "2d": 2, "5d": 5, "1y": 365, "3mo": 92}

        tf   = _tf_map.get(interval)
        days = _days_map.get(period, 5)
        if tf is None:
            return None

        start = datetime.utcnow() - timedelta(days=days)
        req   = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=tf,
            start=start,
        )
        bars = client.get_stock_bars(req)
        if not bars or ticker not in bars.data or not bars.data[ticker]:
            return None

        df = bars.df
        # bars.df is multi-indexed (symbol, timestamp) when multi-ticker; flatten
        if isinstance(df.index, pd.MultiIndex):
            df = df.loc[ticker] if ticker in df.index.get_level_values(0) else df.droplevel(0)
        df.index = pd.to_datetime(df.index, utc=True).tz_localize(None)
        df.columns = [c.title() for c in df.columns]  # open→Open etc.
        if len(df) < 5:
            return None
        return df
    except Exception:
        return None


def _yfinance_bars(ticker: str, period: str, interval: str) -> Optional[pd.DataFrame]:
    """yfinance fallback — used when Alpaca keys are absent or call fails."""
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
        from alpaca.data.requests import StockLatestQuoteRequest

        client = _get_alpaca_data_client()
        if client is None:
            return 0.0, {"error": "no_alpaca_keys"}

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

_atr_cache: dict = {}
_ATR_CACHE_TTL = 300  # 5 minutes

def compute_atr(ticker: str, period: int = 14) -> dict:
    """
    Computes ATR on 5-min bars.
    Returns atr_pct and suggested_stop_pct (1.5× ATR, clamped 0.5%–4%).
    """
    cache_key = (ticker.upper(), period)
    now = time.time()
    if cache_key in _atr_cache:
        cached_ts, cached_result = _atr_cache[cache_key]
        if now - cached_ts < _ATR_CACHE_TTL:
            return cached_result

    try:
        df = _get_bars(ticker, period="5d", interval="5m")
        if df is None or len(df) < period + 1:
            result = {"atr_pct": None, "suggested_stop_pct": None}
            _atr_cache[cache_key] = (now, result)
            return result

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
            result = {"atr_pct": None, "suggested_stop_pct": None}
            _atr_cache[cache_key] = (now, result)
            return result

        atr_pct           = atr / current_price * 100
        suggested_stop_pct = max(0.5, min(4.0, 1.5 * atr_pct))

        if atr_pct < 0.5:
            vol_regime = "low"
        elif atr_pct < 2.0:
            vol_regime = "normal"
        elif atr_pct < 4.0:
            vol_regime = "high"
        else:
            vol_regime = "extreme"

        result = {
            "atr_pct":            round(atr_pct, 4),
            "suggested_stop_pct": round(suggested_stop_pct, 4),
            "atr_raw":            round(atr, 6),
            "current_price":      round(current_price, 4),
            "volatility_regime":  vol_regime,
        }
        _atr_cache[cache_key] = (now, result)
        return result
    except Exception:
        result = {"atr_pct": None, "suggested_stop_pct": None, "volatility_regime": None}
        _atr_cache[cache_key] = (now, result)
        return result


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


# ── Swing score (daily bars — for multi-day holds) ────────────────────────────

def compute_swing_score(ticker: str) -> tuple[float, dict]:
    """
    Daily-bar MACD + RSI + trend for multi-day swing positions.
    Uses period='3mo', interval='1d'.  Returns score -1 to +1.
    """
    try:
        df = _get_bars(ticker, period="3mo", interval="1d")
        if df is None or len(df) < 30:
            return 0.0, {"error": "insufficient_data"}

        close = df["Close"].squeeze()

        # Daily RSI
        rsi       = compute_rsi(close, 14)
        latest_rsi = float(rsi.iloc[-1])
        if latest_rsi < 30:
            rsi_score = 0.7
        elif latest_rsi > 70:
            rsi_score = -0.7
        else:
            rsi_score = (50 - latest_rsi) / 50 * 0.5

        # Daily MACD
        ema_fast = close.ewm(span=12, adjust=False).mean()
        ema_slow = close.ewm(span=26, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        hist = macd_line - signal_line
        latest_hist  = float(hist.iloc[-1])
        prev_hist    = float(hist.iloc[-2]) if len(hist) > 1 else 0.0
        latest_price = float(close.iloc[-1])
        hist_pct     = latest_hist / latest_price * 100 if latest_price else 0.0
        crossed_up   = prev_hist <= 0 < latest_hist
        crossed_down = prev_hist >= 0 > latest_hist
        macd_score   = _clamp(hist_pct / 0.5)
        if crossed_up:
            macd_score = _clamp(macd_score + 0.4)
        elif crossed_down:
            macd_score = _clamp(macd_score - 0.4)

        # 20/50-day trend alignment
        sma20 = float(close.rolling(20).mean().iloc[-1])
        sma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else sma20
        if latest_price > sma20 > sma50:
            trend_score = 0.5
        elif latest_price < sma20 < sma50:
            trend_score = -0.5
        else:
            trend_score = 0.0

        composite = _clamp(rsi_score * 0.35 + macd_score * 0.40 + trend_score * 0.25)
        return composite, {
            "rsi":          round(latest_rsi, 1),
            "rsi_score":    round(rsi_score, 3),
            "macd_hist":    round(latest_hist, 5),
            "macd_score":   round(macd_score, 3),
            "trend_score":  round(trend_score, 3),
            "price":        round(latest_price, 4),
            "sma20":        round(sma20, 4),
            "sma50":        round(sma50, 4),
            "crossed_up":   crossed_up,
            "crossed_down": crossed_down,
        }
    except Exception as e:
        return 0.0, {"error": str(e)[:80]}


# ── Mean reversion (bull-regime ETF swing signal) ─────────────────────────────

def mean_reversion_score(ticker: str, regime_state: RegimeState) -> tuple[float, dict]:
    """
    Connors-style short-term ETF mean reversion.
    It is deliberately disabled outside bull regimes.
    """
    ticker_up = ticker.upper()
    if regime_state.market_regime != "bull":
        return 0.0, {
            "mean_reversion_signal": False,
            "reason": f"disabled_in_{regime_state.market_regime}",
        }
    if ticker_up not in ETF_UNIVERSE:
        return 0.0, {"mean_reversion_signal": False, "reason": "not_etf_universe"}

    try:
        df = yf.download(ticker_up, period="20d", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or df.empty or len(df) < 5:
            return 0.0, {"mean_reversion_signal": False, "error": "insufficient_data"}

        close = df["Close"].squeeze()
        rsi_2 = compute_rsi(close, 2)
        latest_rsi = float(rsi_2.iloc[-1])

        down_days = 0
        for i in range(len(close) - 1, 0, -1):
            if float(close.iloc[i]) < float(close.iloc[i - 1]):
                down_days += 1
            else:
                break

        if latest_rsi < 15 and down_days >= 3:
            score = min(0.85, (15 - latest_rsi) / 15 * 0.85)
            return _clamp(score), {
                "rsi_2": round(latest_rsi, 2),
                "consecutive_down_days": down_days,
                "mean_reversion_signal": True,
                "hold_days": 2,
            }
        return 0.0, {
            "rsi_2": round(latest_rsi, 2),
            "consecutive_down_days": down_days,
            "mean_reversion_signal": False,
        }
    except Exception as e:
        return 0.0, {"mean_reversion_signal": False, "error": str(e)[:80]}


# ── Macro regime classifier ───────────────────────────────────────────────────

_macro_regime_cache: dict = {}
_MACRO_REGIME_TTL = 3600  # 1 hour

# Known FOMC decision dates 2025-2026 (updated annually)
_FOMC_DATES = [
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]

_GEO_KEYWORDS    = ["war", "conflict", "sanctions", "blockade", "strait",
                    "opec", "crisis", "attack", "invasion", "missile"]
ENERGY_TICKERS   = {"XLE", "XOM", "USO", "CVX", "COP", "OXY"}
TECH_TICKERS     = {"QQQ", "NVDA", "MSFT", "GOOGL", "META", "AMZN", "AAPL"}
MOMENTUM_TICKERS = {"QQQ", "NVDA", "COIN", "ARKK"}
SAFE_TICKERS     = {"GLD", "TLT"}
ETF_UNIVERSE     = {"SPY", "QQQ", "VGT", "IWM", "GLD", "TLT", "XOP", "XLF", "XLV", "SMH"}
_TICKER_SECTORS = {
    "SPY": {"market", "equities", "broad_market"},
    "QQQ": {"technology", "tech", "growth"},
    "NVDA": {"technology", "tech", "semiconductors"},
    "AAPL": {"technology", "tech"},
    "MSFT": {"technology", "tech"},
    "COIN": {"crypto", "risk_on"},
    "IBIT": {"crypto", "bitcoin"},
    "XLE": {"energy", "oil"},
    "XOM": {"energy", "oil"},
    "USO": {"energy", "oil"},
    "GLD": {"gold", "safe_haven", "precious_metals"},
    "TLT": {"bonds", "rates", "safe_haven"},
    "XLV": {"healthcare", "defensive"},
    "IWM": {"small_caps", "equities"},
    "XLF": {"financials", "banks"},
    "XLK": {"technology", "tech"},
}


def _pct_change_last_two(df: pd.DataFrame) -> float:
    close = df["Close"].squeeze()
    if len(close) < 2:
        return 0.0
    prev = float(close.iloc[-2])
    last = float(close.iloc[-1])
    return (last - prev) / prev * 100 if prev else 0.0


def _recent_geo_news_velocity() -> dict:
    """
    Approximate 24h geopolitical-news velocity from free yfinance headlines.
    Baseline is configurable because free headline feeds do not expose enough
    stable long-horizon history for a robust per-source baseline.
    """
    baseline = float(os.getenv("GEO_NEWS_BASELINE_24H", "1.0"))
    now = datetime.utcnow()
    recent_hits = 0
    total_recent = 0

    for proxy in ("XLE", "USO", "SPY", "QQQ"):
        try:
            for item in (yf.Ticker(proxy).news or [])[:20]:
                title = (item.get("title") or "").lower()
                provider_time = item.get("providerPublishTime") or item.get("provider_publish_time")
                if provider_time:
                    published = datetime.utcfromtimestamp(int(provider_time))
                    if (now - published).total_seconds() > 24 * 3600:
                        continue
                total_recent += 1
                if any(kw in title for kw in _GEO_KEYWORDS):
                    recent_hits += 1
        except Exception:
            continue

    velocity = recent_hits / max(baseline, 0.1)
    return {
        "geo_recent_hits_24h": recent_hits,
        "geo_total_recent_headlines": total_recent,
        "geo_baseline_24h": baseline,
        "geo_velocity_ratio": round(velocity, 2),
    }


def _fomc_calendar_window() -> Optional[dict]:
    """
    Use FRED's public releases page opportunistically, with known FOMC dates as
    fallback. The FRED page is HTML and needs no API key.
    """
    today = datetime.utcnow().date()
    date_candidates = set(_FOMC_DATES)
    try:
        resp = requests.get("https://fred.stlouisfed.org/releases", timeout=5)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        if "FOMC" in text or "Federal Open Market Committee" in text:
            # The releases page does not consistently expose a structured FOMC
            # calendar, so this confirms reachability and falls through to the
            # maintained fallback dates.
            pass
    except Exception:
        pass

    for date_str in date_candidates:
        try:
            fomc_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        diff = (fomc_date - today).days
        if -1 <= diff <= 1:
            return {"fomc_date": date_str, "days_to_fomc": diff}
    return None


def detect_macro_regime(return_meta: bool = False):
    """
    Returns (regime, metadata) where regime is one of:
    'geopolitical_shock' | 'rate_shift' | 'risk_off' | 'risk_on' | 'normal'

    Checked in priority order; result is cached for 1 hour.
    """
    now_ts = time.time()
    if "macro" in _macro_regime_cache:
        cached_ts, cached_result = _macro_regime_cache["macro"]
        if now_ts - cached_ts < _MACRO_REGIME_TTL:
            return cached_result if return_meta else cached_result[0]

    meta: dict = {}

    # ── 1. Geopolitical shock: energy move + geo news keywords ────────────────
    try:
        uso_df = yf.download("USO", period="5d", interval="1d",
                             progress=False, auto_adjust=True)
        xle_df = yf.download("XLE", period="5d", interval="1d",
                             progress=False, auto_adjust=True)
        oil_ret = 0.0
        if not uso_df.empty and len(uso_df) >= 2:
            oil_ret = _pct_change_last_two(uso_df)
        if abs(oil_ret) <= 5 and not xle_df.empty and len(xle_df) >= 2:
            oil_ret = _pct_change_last_two(xle_df)
        news_velocity = _recent_geo_news_velocity()
        meta.update(news_velocity)
        meta["oil_proxy_change_pct"] = round(oil_ret, 2)

        if abs(oil_ret) > 5 and news_velocity["geo_velocity_ratio"] > 5:
            result = ("geopolitical_shock", meta)
            _macro_regime_cache["macro"] = (now_ts, result)
            return result if return_meta else result[0]
    except Exception:
        pass

    # ── 2. Rate shift: FOMC ±1 day ────────────────────────────────────────────
    try:
        fomc = _fomc_calendar_window()
        if fomc:
            meta.update(fomc)
            result = ("rate_shift", meta)
            _macro_regime_cache["macro"] = (now_ts, result)
            return result if return_meta else result[0]
    except Exception:
        pass

    # ── 3. Risk-off / risk-on: SPY vs TLT daily returns ──────────────────────
    try:
        spy_df = yf.download("SPY", period="5d", interval="1d",
                             progress=False, auto_adjust=True)
        tlt_df = yf.download("TLT", period="5d", interval="1d",
                             progress=False, auto_adjust=True)
        if len(spy_df) >= 2 and len(tlt_df) >= 2:
            spy_ret = _pct_change_last_two(spy_df)
            tlt_ret = _pct_change_last_two(tlt_df)
            meta["spy_ret_pct"] = round(spy_ret, 2)
            meta["tlt_ret_pct"] = round(tlt_ret, 2)

            if tlt_ret > 1.0 and spy_ret < -1.0:
                result = ("risk_off", meta)
                _macro_regime_cache["macro"] = (now_ts, result)
                return result if return_meta else result[0]
            if spy_ret > 1.0 and tlt_ret <= 0.0:
                result = ("risk_on", meta)
                _macro_regime_cache["macro"] = (now_ts, result)
                return result if return_meta else result[0]
    except Exception:
        pass

    # ── 4. Yield curve inversion: deeply inverted → risk_off ─────────────────
    try:
        t10y2y = _get_fred_value("T10Y2Y")
        if t10y2y is not None:
            meta["yield_curve_t10y2y"] = round(t10y2y, 3)
            if t10y2y < -0.5:
                result = ("risk_off", {**meta, "yield_curve_trigger": True})
                _macro_regime_cache["macro"] = (now_ts, result)
                return result if return_meta else result[0]
    except Exception:
        pass

    result = ("normal", meta)
    _macro_regime_cache["macro"] = (now_ts, result)
    return result if return_meta else result[0]


# ── LLM macro shock scanner ───────────────────────────────────────────────────

_shock_scan_cache: dict = {}
_SHOCK_SCAN_TTL = 900  # 15 minutes
_shock_scan_calls: list[float] = []
_last_shock_emit_key: str = ""


def latest_macro_headlines(limit_per_ticker: int = 5) -> list[str]:
    headlines = []
    for ticker in ("SPY", "XLE", "GLD"):
        try:
            for item in (yf.Ticker(ticker).news or [])[:limit_per_ticker]:
                title = (item.get("title") or "").strip()
                if title:
                    headlines.append(f"{ticker}: {title}")
        except Exception:
            continue
    return headlines[:15]


def _shock_scan_allowed(now_ts: float) -> bool:
    global _shock_scan_calls
    _shock_scan_calls = [ts for ts in _shock_scan_calls if now_ts - ts < 3600]
    return len(_shock_scan_calls) < 4


def _normal_shock_result(reason: str = "normal") -> dict:
    return {
        "shock_detected": False,
        "classification": "NORMAL",
        "affected_sectors": [],
        "direction": "mixed",
        "reason": reason,
    }


def _parse_jsonish(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


def scan_for_macro_shock(news_headlines: list[str]) -> dict:
    """
    Single cached Haiku-classification pass over macro headlines.
    Falls back to NORMAL if the LLM provider is unavailable.
    """
    now_ts = time.time()
    if "latest" in _shock_scan_cache:
        cached_ts, cached_result = _shock_scan_cache["latest"]
        if now_ts - cached_ts < _SHOCK_SCAN_TTL:
            return cached_result

    if not news_headlines:
        result = _normal_shock_result("no_headlines")
        _shock_scan_cache["latest"] = (now_ts, result)
        return result

    if not _shock_scan_allowed(now_ts):
        result = _normal_shock_result("shock_scan_hourly_limit")
        _shock_scan_cache["latest"] = (now_ts, result)
        return result

    prompt = (
        "Classify headlines NORMAL, SIGNIFICANT, or SHOCK. "
        "SHOCK=geopolitical conflict, circuit breaker, emergency central bank, pandemic. "
        "JSON only: {classification,reason,affected_sectors,direction}. "
        + " | ".join(news_headlines[:15])[:650]
    )

    try:
        from groq import Groq
        client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        response = client.chat.completions.create(
            model=os.getenv("GROQ_SHOCK_MODEL", "llama-3.1-8b-instant"),
            max_tokens=120,
            temperature=0.1,
            messages=[
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": prompt},
            ],
        )
        raw = response.choices[0].message.content

        parsed = _parse_jsonish(raw)
        classification = str(parsed.get("classification", "NORMAL")).upper()
        affected = parsed.get("affected_sectors") or []
        if isinstance(affected, str):
            affected = [affected]
        result = {
            "shock_detected": classification == "SHOCK",
            "classification": classification,
            "affected_sectors": [str(s).lower() for s in affected],
            "direction": str(parsed.get("direction", "mixed")).lower(),
            "reason": str(parsed.get("reason", ""))[:240],
        }
        _shock_scan_calls.append(now_ts)
    except Exception as e:
        result = _normal_shock_result(f"shock_scan_unavailable: {str(e)[:100]}")

    _shock_scan_cache["latest"] = (now_ts, result)
    return result


def ticker_matches_shock(ticker: str, affected_sectors: list[str]) -> bool:
    affected = {str(s).lower().replace(" ", "_") for s in affected_sectors or []}
    ticker_up = ticker.upper()
    sectors = _TICKER_SECTORS.get(ticker_up, set()) | {ticker_up.lower()}
    return bool(affected & sectors)


def _emit_shock_side_effects(shock_result: dict):
    """Best-effort DB/Telegram emission, deduped per classification+reason."""
    global _last_shock_emit_key
    if not shock_result.get("shock_detected"):
        return
    key = f"{shock_result.get('classification')}:{shock_result.get('reason')}"
    if key == _last_shock_emit_key:
        return
    _last_shock_emit_key = key
    try:
        from database.client import log_event
        log_event("SIGNAL", "macro_shock_detected", shock_result)
    except Exception:
        pass
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if token and chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": (
                        "Macro shock detected\n"
                        f"Classification: {shock_result.get('classification')}\n"
                        f"Direction: {shock_result.get('direction')}\n"
                        f"Affected: {', '.join(shock_result.get('affected_sectors') or [])}\n"
                        f"Reason: {shock_result.get('reason')}"
                    ),
                },
                timeout=10,
            )
        except Exception:
            pass


# ── Regime Detection ──────────────────────────────────────────────────────────

_regime_state_cache: dict = {}
_REGIME_STATE_TTL = 4 * 3600

# ── FRED macro data cache ─────────────────────────────────────────────────────
_fred_cache: dict = {}
_FRED_CACHE_TTL = 6 * 3600   # 6 hours — FRED updates once daily


def _get_fred_value(series_id: str) -> Optional[float]:
    """
    Fetch the latest observation for a FRED series.
    Requires FRED_API_KEY env var (free at fred.stlouisfed.org).
    Returns None on failure so callers can degrade gracefully.
    """
    now_ts = time.time()
    if series_id in _fred_cache:
        cached_ts, cached_val = _fred_cache[series_id]
        if now_ts - cached_ts < _FRED_CACHE_TTL:
            return cached_val

    api_key = os.getenv("FRED_API_KEY")
    if not api_key:
        return None

    try:
        url = (
            "https://api.stlouisfed.org/fred/series/observations"
            f"?series_id={series_id}&api_key={api_key}"
            "&limit=5&sort_order=desc&file_type=json"
        )
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        obs  = resp.json().get("observations", [])
        # Most recent non-missing value
        for o in obs:
            if o.get("value", ".") != ".":
                val = float(o["value"])
                _fred_cache[series_id] = (now_ts, val)
                return val
    except Exception:
        pass
    return None


def detect_regime(ticker: str = "SPY") -> RegimeState:
    """
    Returns a richer market/intraday regime state.
    Market regime uses SPY vs 200-day SMA; intraday regime preserves the
    existing VIX/trend classifier.
    """
    cache_key = ticker.upper()
    now_ts = time.time()
    if cache_key in _regime_state_cache:
        cached_ts, cached_state = _regime_state_cache[cache_key]
        if now_ts - cached_ts < _REGIME_STATE_TTL:
            return cached_state

    # VIX: prefer FRED VIXCLS (official, no scraping); fall back to yfinance
    vix = _get_fred_value("VIXCLS")
    if vix is None:
        try:
            vix_df = yf.download("^VIX", period="2d", interval="1h",
                                 progress=False, auto_adjust=True)
            vix = float(vix_df["Close"].squeeze().iloc[-1]) if not vix_df.empty else 20.0
        except Exception:
            vix = 20.0

    # Yield curve: T10Y2Y from FRED (10yr minus 2yr Treasury spread)
    yield_curve       = _get_fred_value("T10Y2Y") or 0.0
    if yield_curve < -0.2:
        yield_curve_state = "inverted"
    elif yield_curve <= 0.2:
        yield_curve_state = "flat"
    else:
        yield_curve_state = "normal"

    market_regime = "transitioning"
    sma200_price = 0.0
    price_vs_sma200_pct = 0.0
    try:
        spy_df = yf.download("SPY", period="1y", interval="1d",
                             progress=False, auto_adjust=True)
        if not spy_df.empty and len(spy_df) >= 200:
            close = spy_df["Close"].squeeze()
            spy_price = float(close.iloc[-1])
            sma200_price = float(close.rolling(200).mean().iloc[-1])
            if sma200_price > 0:
                price_vs_sma200_pct = (spy_price - sma200_price) / sma200_price * 100
            if price_vs_sma200_pct > 0:
                market_regime = "bull"
            elif price_vs_sma200_pct < -2:
                market_regime = "bear"
            else:
                market_regime = "transitioning"
    except Exception:
        pass

    trend_score = 0.0
    trend_threshold = 1.5
    trend_mean_return = 0.0
    trend_std_return = 0.0

    # High-vol gate: VIX > 30 OR deeply inverted curve (< -0.5) signals stress
    _high_vol = vix > 30 or yield_curve < -0.5
    if vix > 30 and yield_curve < -0.5:
        regime_reason = "vix_high_inverted_curve"
    elif vix > 30:
        regime_reason = "vix_high"
    elif yield_curve < -0.5:
        regime_reason = "inverted_curve"
    else:
        regime_reason = "default"

    if _high_vol:
        intraday_regime = "high_vol"
    else:
        df = _get_bars(ticker, period="5d", interval="15m")
        if df is None:
            intraday_regime = "ranging"
            regime_reason = "insufficient_intraday_data"
        else:
            close  = df["Close"].squeeze()
            returns   = close.pct_change().dropna()
            std_ret   = float(returns.rolling(14).std().iloc[-1])
            mean_ret  = float(returns.rolling(14).mean().iloc[-1])
            if not np.isfinite(std_ret) or not np.isfinite(mean_ret):
                std_ret = 0.0
                mean_ret = 0.0
                regime_reason = "insufficient_return_window"
            trend_score = abs(mean_ret) / (std_ret + 1e-9)
            intraday_regime = "trending" if trend_score > 1.5 else "ranging"
            trend_mean_return = mean_ret
            trend_std_return = std_ret
            if regime_reason != "insufficient_return_window":
                regime_reason = "trend_score_above_threshold" if intraday_regime == "trending" else "trend_score_below_threshold"

    state = RegimeState(
        market_regime=market_regime,
        intraday_regime=intraday_regime,
        vix=round(vix, 2),
        sma200_price=round(sma200_price, 4),
        price_vs_sma200_pct=round(price_vs_sma200_pct, 3),
        trend_score=round(trend_score, 4),
        trend_threshold=trend_threshold,
        trend_mean_return=round(trend_mean_return, 8),
        trend_std_return=round(trend_std_return, 8),
        regime_reason=regime_reason,
        yield_curve=round(yield_curve, 3),
        yield_curve_state=yield_curve_state,
    )
    _regime_state_cache[cache_key] = (now_ts, state)
    return state


# ── Momentum swing detector ───────────────────────────────────────────────────

def detect_momentum_swing(
    ticker: str,
    signal_result: dict,
    regime_state: "RegimeState",
    profile: dict = None,
) -> dict:
    """
    Detects when a ticker has a high-conviction multi-day momentum setup.
    All 5 core conditions must align simultaneously.
    Bear regime and macro shock are absolute blocks regardless of signal strength.
    """
    sigs = signal_result.get("signals", {})

    # Gate 1: Must be bull regime — absolute block
    if regime_state.market_regime != "bull":
        return {"swing_detected": False, "disqualifiers": ["not_bull_regime"]}

    # Gate 2: No macro shock active
    if signal_result.get("shock_detected", False):
        return {"swing_detected": False, "disqualifiers": ["shock_active"]}

    # Gate 3: Not within 3 days of earnings
    earn_meta = sigs.get("earnings_proximity", {}).get("meta", {})
    days_to_earn = earn_meta.get("days_to_earnings")
    if days_to_earn is not None and days_to_earn <= 3:
        return {"swing_detected": False, "disqualifiers": [f"earnings_in_{days_to_earn}d"]}

    # Collect signal scores
    composite    = signal_result.get("composite_score", 0)
    rel_strength = sigs.get("relative_strength",  {}).get("score", 0)
    macd         = sigs.get("macd_crossover",     {}).get("score", 0)
    tape         = sigs.get("tape_aggression",    {}).get("score", 0)
    rsi          = sigs.get("rsi_divergence",     {}).get("score", 0)
    bollinger    = sigs.get("bollinger_squeeze",  {}).get("score", 0)

    conditions = {
        "composite_high":     composite > 0.60,
        "rel_strength":       rel_strength > 0.35,
        "macd_bullish":       macd > 0.35,
        "tape_bullish":       tape > 0.30,
        "rsi_not_overbought": rsi > -0.20,
    }

    bonus_conditions = {
        "bollinger_breakout": bollinger > 0.50,
        "macd_crossover":     macd > 0.70,
        "strong_rs":          rel_strength > 0.60,
    }

    passed  = [k for k, v in conditions.items() if v]
    failed  = [k for k, v in conditions.items() if not v]
    bonuses = [k for k, v in bonus_conditions.items() if v]

    if failed:
        return {
            "swing_detected":    False,
            "disqualifiers":     failed,
            "conditions_met":    len(passed),
            "conditions_needed": 5,
        }

    base_conviction  = (composite + rel_strength + macd + tape) / 4
    bonus_conviction = len(bonuses) * 0.05
    conviction       = min(1.0, base_conviction + bonus_conviction)

    # Profile conviction threshold gate
    min_conviction = float((profile or {}).get("swing_conviction_threshold", 0.60))
    if conviction < min_conviction:
        return {
            "swing_detected": False,
            "disqualifiers":  [f"conviction_{conviction:.2f}_below_threshold_{min_conviction:.2f}"],
        }

    # Hold duration: scale with conviction
    if conviction > 0.80:
        hold_days = 5
    elif conviction > 0.70:
        hold_days = 4
    else:
        hold_days = 3

    # Respect profile max_swing_hold_days
    max_hold_days = int((profile or {}).get("max_swing_hold_days", 5))
    hold_days = min(hold_days, max_hold_days)

    return {
        "swing_detected":  True,
        "conviction":      round(conviction, 3),
        "hold_days":       hold_days,
        "hold_minutes":    hold_days * 390,
        "stop_multiplier": 2.5,
        "reasons":         passed + bonuses,
        "bonus_signals":   bonuses,
    }


# ── Master signal composer ────────────────────────────────────────────────────

def compute_all_signals(ticker: str, weights: dict,
                        regime_state: RegimeState = None,
                        shock_result: dict = None) -> dict:
    """
    Runs all 10 signals + ATR, applies profile weights, returns composite score
    plus full metadata for logging and display.
    """
    results = {}
    regime_state = regime_state or detect_regime()

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

    try:
        s11, m11 = mean_reversion_score(ticker, regime_state)
    except Exception:
        s11, m11 = 0.0, {"mean_reversion_signal": False, "error": "signal_crashed"}

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
    results["mean_reversion"]       = {"score": s11, "meta": m11}

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
        "mean_reversion":      s11,
    }
    effective_weights = dict(weights or {})
    if regime_state.market_regime == "bull":
        for key in ("tape_aggression", "macd_crossover", "relative_strength"):
            effective_weights[key] = effective_weights.get(key, 0.0) * 1.2
        effective_weights["mean_reversion"] = max(effective_weights.get("mean_reversion", 0.0), 0.12)
    elif regime_state.market_regime == "bear":
        for key in ("rsi_divergence", "vwap_deviation", "bollinger_squeeze"):
            effective_weights[key] = effective_weights.get(key, 0.0) * 1.2
        effective_weights["mean_reversion"] = 0.0
    else:
        effective_weights["mean_reversion"] = 0.0

    high_vol_multiplier = 0.8 if regime_state.intraday_regime == "high_vol" else 1.0
    if regime_state.intraday_regime == "high_vol":
        effective_weights = {k: v * 0.8 for k, v in effective_weights.items()}

    weight_total = sum(max(0.0, effective_weights.get(k, 0.0)) for k in weighted_signals)
    if weight_total <= 0:
        weight_total = 1.0
    composite = sum(
        score * (max(0.0, effective_weights.get(name, 0.0)) / weight_total)
        for name, score in weighted_signals.items()
    )
    composite *= high_vol_multiplier
    earnings_multiplier = m8.get("earnings_multiplier", 1.0)
    composite *= earnings_multiplier
    composite = _clamp(composite)

    # Macro regime modifier
    macro_regime, macro_meta = detect_macro_regime(return_meta=True)
    macro_multiplier = 1.0
    ticker_up = ticker.upper()
    if macro_regime == "geopolitical_shock":
        if ticker_up in ENERGY_TICKERS:
            macro_multiplier = 1.4   # amplify energy signal in oil crisis
        elif ticker_up in TECH_TICKERS:
            macro_multiplier = 0.7   # dampen tech — capital rotating away
    elif macro_regime == "risk_off":
        if ticker_up in SAFE_TICKERS:
            macro_multiplier = 1.3   # boost GLD / TLT
        else:
            macro_multiplier = 0.85
    elif macro_regime == "risk_on":
        if ticker_up in MOMENTUM_TICKERS:
            macro_multiplier = 1.2   # boost QQQ, NVDA, COIN

    composite = _clamp(composite * macro_multiplier)

    shock_result = shock_result or _normal_shock_result()
    shock_multiplier = 1.0
    shock_override = False
    if shock_result.get("shock_detected"):
        _emit_shock_side_effects(shock_result)
        shock_override = True
        if ticker_matches_shock(ticker, shock_result.get("affected_sectors", [])):
            shock_multiplier = 1.5
        else:
            shock_multiplier = 0.5
        composite = _clamp(composite * shock_multiplier)

    # Dividend proximity overlay — advisory, read from 1hr cache (no re-fetch per ticker)
    div_opportunity = None
    try:
        from backend.dividends.scanner import get_cached_dividend_scan
        div_opps  = get_cached_dividend_scan()
        div_match = next((d for d in div_opps if d["ticker"] == ticker), None)
        if div_match:
            div_opportunity = div_match
            # Mild positive lean when composite already bullish — never forces a buy
            if composite > 0.2:
                composite = min(1.0, composite * (1 + div_match["opportunity_score"] * 0.15))
    except Exception:
        pass

    return {
        "ticker":              ticker,
        "composite_score":     round(composite, 4),
        "regime":              regime_state.intraday_regime,
        "regime_state":        regime_state.to_dict(),
        "regime_bull_bear":    regime_state.market_regime,
        "macro_regime":        macro_regime,
        "macro_multiplier":    macro_multiplier,
        "macro_meta":          macro_meta,
        "shock_detected":      bool(shock_result.get("shock_detected", False)),
        "shock_classification": shock_result.get("classification", "NORMAL"),
        "shock_result":        shock_result,
        "shock_override":      shock_override,
        "shock_multiplier":    shock_multiplier,
        "signals":             results,
        "weights_used":        effective_weights,
        "earnings_multiplier": earnings_multiplier,
        "atr_data":            atr_data,
        "current_price":       atr_data.get("current_price"),
        "atr_stop_multiplier": 2.0 if regime_state.intraday_regime == "high_vol" else 1.5,
        "mean_reversion_signal": bool(m11.get("mean_reversion_signal")),
        "mean_reversion_meta": m11,
        "dividend_opportunity": div_opportunity,
        "computed_at":         datetime.utcnow().isoformat(),
    }
