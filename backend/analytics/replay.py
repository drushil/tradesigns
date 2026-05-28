"""
backend/analytics/replay.py
Post-trade and blocked-opportunity replay engine.

Fetches historical price data for past signals and trade exits to
measure counterfactual outcomes — did a blocked signal miss a runner?
did an early exit leave money on the table?

Depends on:
  - stdlib + yfinance
  - backend.runtime.env  (env helpers)
  - database.client      (DB read/write)
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Optional

from backend.runtime.env import _env_bool, _env_int, _env_float, _env_value
from database.client import (
    get_unchecked_blocked_opportunities,
    update_blocked_opportunity_replay,
    get_unchecked_closed_trades_for_replay,
    update_trade_post_exit_replay,
    get_unscored_advisory_signals,
    update_advisory_signal_replay,
    log_event,
)


# ---------------------------------------------------------------------------
# Time parsing helper
# ---------------------------------------------------------------------------

def _parse_supabase_time(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Price-window fetcher (yfinance 1m bars)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Advisory forward-return replay
# ---------------------------------------------------------------------------

_ADVISORY_FORWARD_WINDOWS = (5, 15, 30, 60)


def _advisory_reference_price(signal: dict) -> float:
    """Use the advisory entry band midpoint as the replay reference price."""
    try:
        manual_entry = float(signal.get("manual_entry_price") or 0)
        if manual_entry > 0:
            return manual_entry
    except (TypeError, ValueError):
        pass

    try:
        entry_min = float(signal.get("entry_min") or 0)
        entry_max = float(signal.get("entry_max") or 0)
    except (TypeError, ValueError):
        return 0.0
    if entry_min > 0 and entry_max > 0:
        return (entry_min + entry_max) / 2.0
    return entry_min or entry_max or 0.0


def _advisory_price_window(ticker: str, side: str, start_at: datetime,
                           reference_price: float, windows: tuple[int, ...],
                           period: str = "5d") -> dict:
    import yfinance as yf

    ticker = str(ticker or "").upper()
    side = str(side or "BUY").upper()
    if not ticker or not start_at:
        return {}
    if start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=timezone.utc)

    try:
        reference_price = float(reference_price)
    except (TypeError, ValueError):
        reference_price = 0.0
    if reference_price <= 0:
        return {}

    now_utc = datetime.now(timezone.utc)
    elapsed_windows = [
        minutes for minutes in windows
        if now_utc >= start_at + timedelta(minutes=minutes)
    ]
    if not elapsed_windows:
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

    last_elapsed = max(elapsed_windows)
    end_at = min(now_utc, start_at + timedelta(minutes=last_elapsed))
    window = bars[(bars.index >= start_at) & (bars.index <= end_at)]
    if window.empty:
        return {}

    close_series = window["Close"].squeeze()
    high_series = window["High"].squeeze()
    low_series = window["Low"].squeeze()
    forward_returns = {}
    bars_by_window = {}
    for minutes in elapsed_windows:
        horizon_end = start_at + timedelta(minutes=minutes)
        horizon_window = window[window.index <= horizon_end]
        if horizon_window.empty:
            continue
        horizon_close = float(horizon_window["Close"].squeeze().iloc[-1])
        if side == "SELL":
            ret = (reference_price - horizon_close) / reference_price * 100
        else:
            ret = (horizon_close - reference_price) / reference_price * 100
        forward_returns[minutes] = round(ret, 4)
        bars_by_window[str(minutes)] = int(len(horizon_window))

    if not forward_returns:
        return {}

    if side == "SELL":
        max_favorable = (reference_price - float(low_series.min())) / reference_price * 100
        max_adverse = (reference_price - float(high_series.max())) / reference_price * 100
        close_after = (reference_price - float(close_series.iloc[-1])) / reference_price * 100
    else:
        max_favorable = (float(high_series.max()) - reference_price) / reference_price * 100
        max_adverse = (float(low_series.min()) - reference_price) / reference_price * 100
        close_after = (float(close_series.iloc[-1]) - reference_price) / reference_price * 100

    return {
        "forward_returns": forward_returns,
        "max_favorable_pct": round(max_favorable, 4),
        "max_adverse_pct": round(max_adverse, 4),
        "close_after_pct": round(close_after, 4),
        "bars_seen": int(len(window)),
        "bars_by_window": bars_by_window,
        "reference_price": round(reference_price, 4),
        "start": start_at.isoformat(),
        "end": end_at.isoformat(),
        "side": side,
    }


def _replay_one_advisory_signal(signal: dict) -> dict:
    ticker = str(
        signal.get("data_symbol")
        or signal.get("primary_symbol")
        or ""
    ).upper()
    side = str(signal.get("side") or "BUY").upper()
    created_at = _parse_supabase_time(signal.get("created_at"))
    if not ticker or not created_at:
        return {}

    reference_price = _advisory_reference_price(signal)
    replay = _advisory_price_window(
        ticker, side, created_at, reference_price, _ADVISORY_FORWARD_WINDOWS, period="5d"
    )
    now_utc = datetime.now(timezone.utc)
    all_windows_elapsed = now_utc >= created_at + timedelta(minutes=max(_ADVISORY_FORWARD_WINDOWS))
    if not replay:
        payload = {
            "status": "skipped_no_bars" if all_windows_elapsed else "pending_no_bars",
            "windows_minutes": list(_ADVISORY_FORWARD_WINDOWS),
            "ticker": ticker,
            "side": side,
            "reference_price": round(reference_price, 4) if reference_price else None,
            "created_at": created_at.isoformat(),
        }
        return {
            "forward_scored_at": now_utc.isoformat() if all_windows_elapsed else None,
            "advisory_replay_json": payload,
        }

    forward_returns = replay.get("forward_returns") or {}
    complete = max(_ADVISORY_FORWARD_WINDOWS) in forward_returns
    payload = {
        "status": "complete" if complete else "partial",
        "windows_minutes": list(_ADVISORY_FORWARD_WINDOWS),
        "available_windows": sorted(int(k) for k in forward_returns.keys()),
        "bars_seen": replay.get("bars_seen"),
        "bars_by_window": replay.get("bars_by_window"),
        "reference_price": replay.get("reference_price"),
        "start": replay.get("start"),
        "end": replay.get("end"),
        "side": side,
        "alert_stage": (signal.get("signal_json") or {}).get("alert_stage"),
        "grade": signal.get("grade"),
        "status_at_alert": signal.get("status"),
    }
    return {
        "forward_return_5m": forward_returns.get(5),
        "forward_return_15m": forward_returns.get(15),
        "forward_return_30m": forward_returns.get(30),
        "forward_return_60m": forward_returns.get(60),
        "forward_scored_at": now_utc.isoformat() if complete else None,
        "max_favorable_pct": replay.get("max_favorable_pct"),
        "max_adverse_pct": replay.get("max_adverse_pct"),
        "close_after_pct": replay.get("close_after_pct"),
        "advisory_replay_json": payload,
    }


def _replay_advisory_signals():
    if not _env_bool("ADVISORY_REPLAY_ENABLED", True):
        return

    min_age = _env_int("ADVISORY_REPLAY_MIN_AGE_MINUTES", 5)
    limit = _env_int("ADVISORY_REPLAY_LIMIT", 10)
    max_age_days = _env_int("ADVISORY_REPLAY_MAX_AGE_DAYS", 4)
    signals = get_unscored_advisory_signals(
        min_age_minutes=min_age,
        limit=limit,
        max_age_days=max_age_days,
    )
    if not signals:
        return

    checked = 0
    updated = 0
    finalized = 0
    for signal in signals:
        checked += 1
        try:
            replay = _replay_one_advisory_signal(signal)
            if not replay:
                continue
            result = update_advisory_signal_replay(signal.get("id"), replay)
            if not result.get("error"):
                updated += 1
                if replay.get("forward_scored_at"):
                    finalized += 1
        except Exception as e:
            log_event("WARN", "advisory_replay_failed", {
                "id": signal.get("id"),
                "symbol": signal.get("data_symbol"),
                "error": str(e)[:160],
            })
    if checked:
        log_event("INFO", "advisory_replay_complete", {
            "checked": checked,
            "updated": updated,
            "finalized": finalized,
            "min_age_minutes": min_age,
            "max_age_days": max_age_days,
        })


# ---------------------------------------------------------------------------
# Blocked-opportunity replay
# ---------------------------------------------------------------------------

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
            "oldest_created_at": min(
                (str(o.get("created_at")) for o in opportunities if o.get("created_at")),
                default=None,
            ),
            "newest_created_at": max(
                (str(o.get("created_at")) for o in opportunities if o.get("created_at")),
                default=None,
            ),
        })


# ---------------------------------------------------------------------------
# Closed-trade exit replay
# ---------------------------------------------------------------------------

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
