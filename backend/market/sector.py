"""
backend/market/sector.py
Sector-universe configuration, momentum snapshot, and related helpers.

Extracted from backend/agent.py.  All logic is intentionally unchanged.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import tomllib
except Exception:  # pragma: no cover - Python < 3.11 fallback
    try:
        import tomli as tomllib
    except Exception:
        tomllib = None

from backend.runtime.env import (_env_value, _env_int, _env_float, _env_bool)
from database.client import log_event, get_logs
import backend.runtime.state as state


# ---------------------------------------------------------------------------
# Default sector universe (hardcoded fallback)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

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
        path = Path(__file__).resolve().parents[2] / path
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


# ---------------------------------------------------------------------------
# Module-level derived constants (computed at import time)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Ticker classification helpers
# ---------------------------------------------------------------------------

def _default_ticker_universe() -> str:
    core = []
    for sector_name, sector in (_SECTOR_UNIVERSE.get("sectors") or {}).items():
        if str(sector_name).lower() not in _ACTIVE_SECTORS:
            continue
        core.extend(_normalize_ticker_list(sector.get("core", [])))
    if core:
        return ",".join(dict.fromkeys(core))
    return "SPY,QQQ,GLD"


def _ticker_theme(ticker: str) -> str:
    ticker = str(ticker or "").upper()
    for theme, members in _THEME_MAP.items():
        if ticker in members:
            return theme
    return "other"


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


# ---------------------------------------------------------------------------
# Return / momentum helpers
# ---------------------------------------------------------------------------

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


def _return_pct_from_bars(ticker: str, period: str = "5d", interval: str = "1d") -> Optional[float]:
    return _return_pcts_from_bars([ticker], period=period, interval=interval).get(str(ticker or "").upper())


def _sector_proxy_symbols(theme: str, proxy: str) -> list[str]:
    basket = _THEME_PROXY_BASKETS.get(theme) or []
    return basket or [proxy]


def _sector_proxy_return(theme: str, proxy: str, returns: dict[str, float]) -> Optional[float]:
    symbols = _sector_proxy_symbols(theme, proxy)
    values = [returns[s] for s in symbols if s in returns]
    if not values:
        return None
    return sum(values) / len(values)


# ---------------------------------------------------------------------------
# Sector momentum snapshot
# ---------------------------------------------------------------------------

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
    _cycle_composites = state._cycle_composites
    for theme, proxy in _THEME_PROXIES.items():
        theme_return = _sector_proxy_return(theme, proxy, proxy_returns)
        if theme_return is None:
            continue
        relative = theme_return - spy_return
        theme_threshold = float(_sector_setting(theme, "min_5d_return_for_bonus_pct", leadership_threshold))
        theme_max_bonus = float(_sector_setting(theme, "leadership_bonus", max_bonus))
        theme_max_bonus = min(max_bonus, max(0.0, theme_max_bonus))
        internal_alignment = None
        if (
            theme == "crypto"
            and bool(profile.get("crypto_internal_align_enabled", True))
        ):
            cluster = ["IBIT", "COIN", "MSTR"]
            values = {
                symbol: float(_cycle_composites.get(symbol) or 0)
                for symbol in cluster
                if symbol in _cycle_composites
            }
            if values:
                avg = sum(values.values()) / len(values)
                direction = 1 if avg >= 0 else -1
                aligned = [symbol for symbol, score in values.items() if score * direction > 0.05]
                if len(aligned) >= 3:
                    leader = True
                    bonus = theme_max_bonus
                    multiplier = round(1.0 + bonus, 4)
                elif len(aligned) >= 2:
                    leader = False
                    bonus = 0.0
                    multiplier = 0.7
                else:
                    leader = False
                    bonus = 0.0
                    multiplier = 1.0
                internal_alignment = {
                    "enabled": True,
                    "cluster": cluster,
                    "observed": values,
                    "aligned": aligned,
                    "direction": "bullish" if direction > 0 else "bearish",
                    "aligned_count": len(aligned),
                }
            else:
                leader = relative >= theme_threshold
                bonus = min(theme_max_bonus, max(0.0, relative / 20.0)) if leader else 0.0
                multiplier = round(1.0 + bonus, 4)
        else:
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
        if internal_alignment:
            themes[theme]["internal_alignment"] = internal_alignment
        if multiplier != 1.0:
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


# ---------------------------------------------------------------------------
# Candidate enrichment
# ---------------------------------------------------------------------------

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
    if multiplier != 1.0:
        setup_context["candidate_rank_score"] = round(base_rank * multiplier, 4)
    return candidate


# ---------------------------------------------------------------------------
# Dynamic universe / shadow recommendations
# ---------------------------------------------------------------------------

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
