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
import numpy as np
import pandas as pd


def _col_values(df_or_series, col: str) -> np.ndarray:
    """
    Extract a column from a yfinance result as a 1-D numpy array.

    yfinance ≥0.2 with a single ticker returns MultiIndex columns
    (Close, NVDA).  Calling ['Close'] on that gives a DataFrame not a Series,
    and .squeeze() on a 1-row DataFrame collapses to a scalar which breaks
    .iloc/.min()/.max().  This helper always returns a flat ndarray so callers
    can use plain array indexing (arr[0], arr[-1], arr.min(), arr.max()).
    """
    obj = df_or_series[col]
    if isinstance(obj, pd.DataFrame):
        obj = obj.iloc[:, 0]   # take first (only) ticker column
    arr = np.asarray(obj, dtype=float).ravel()
    return arr

from backend.runtime.env import _env_bool, _env_int, _env_float, _env_value
from database.client import (
    get_unchecked_blocked_opportunities,
    update_blocked_opportunity_replay,
    get_unchecked_closed_trades_for_replay,
    update_trade_post_exit_replay,
    get_unscored_advisory_signals,
    get_advisory_signals_needing_5d_score,
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

    close_arr = _col_values(window, "Close")
    high_arr  = _col_values(window, "High")
    low_arr   = _col_values(window, "Low")
    if reference_price <= 0:
        reference_price = float(close_arr[0])
    if reference_price <= 0:
        return {}

    if action == "SELL":
        max_favorable = (reference_price - float(low_arr.min()))  / reference_price * 100
        max_adverse   = (reference_price - float(high_arr.max())) / reference_price * 100
        close_after   = (reference_price - float(close_arr[-1]))  / reference_price * 100
    else:
        max_favorable = (float(high_arr.max())  - reference_price) / reference_price * 100
        max_adverse   = (float(low_arr.min())   - reference_price) / reference_price * 100
        close_after   = (float(close_arr[-1])   - reference_price) / reference_price * 100

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
# 5-day horizon is scored in a separate pass (needs 5 calendar days of data).
_ADVISORY_5D_MINUTES = 5 * 24 * 60  # 7200 minutes — used only as an age gate


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

    close_arr = _col_values(window, "Close")
    high_arr  = _col_values(window, "High")
    low_arr   = _col_values(window, "Low")
    forward_returns = {}
    bars_by_window = {}
    for minutes in elapsed_windows:
        horizon_end = start_at + timedelta(minutes=minutes)
        horizon_window = window[window.index <= horizon_end]
        if horizon_window.empty:
            continue
        horizon_close = float(_col_values(horizon_window, "Close")[-1])
        if side == "SELL":
            ret = (reference_price - horizon_close) / reference_price * 100
        else:
            ret = (horizon_close - reference_price) / reference_price * 100
        forward_returns[minutes] = round(ret, 4)
        bars_by_window[str(minutes)] = int(len(horizon_window))

    if not forward_returns:
        return {}

    if side == "SELL":
        max_favorable = (reference_price - float(low_arr.min()))  / reference_price * 100
        max_adverse   = (reference_price - float(high_arr.max())) / reference_price * 100
        close_after   = (reference_price - float(close_arr[-1]))  / reference_price * 100
    else:
        max_favorable = (float(high_arr.max())  - reference_price) / reference_price * 100
        max_adverse   = (float(low_arr.min())   - reference_price) / reference_price * 100
        close_after   = (float(close_arr[-1])   - reference_price) / reference_price * 100

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
    fwd_60m = forward_returns.get(60)
    # fwd_60m is already sign-adjusted for side (positive = thesis correct for both
    # BUY and SELL) because _advisory_price_window returns (ref - close) for SELL.
    direction_correct_60m: Optional[bool] = (fwd_60m > 0) if fwd_60m is not None else None

    # Derive pick_outcome_bucket from the existing replay columns on the signal.
    # We use target_hit_first / stop_hit_first / fwd_60m as a proxy here;
    # the authoritative bucket may be overwritten by the 5d scorer later.
    target_hit = signal.get("target_hit_first")
    stop_hit = signal.get("stop_hit_first")
    if target_hit:
        bucket = "tp1_hit"
    elif stop_hit:
        bucket = "stop_hit"
    elif complete and fwd_60m is not None:
        bucket = "expired_positive" if fwd_60m > 0 else "expired_negative"
    else:
        bucket = "pending"

    result: dict = {
        "forward_return_5m": forward_returns.get(5),
        "forward_return_15m": forward_returns.get(15),
        "forward_return_30m": forward_returns.get(30),
        "forward_return_60m": fwd_60m,
        "forward_scored_at": now_utc.isoformat() if complete else None,
        "max_favorable_pct": replay.get("max_favorable_pct"),
        "max_adverse_pct": replay.get("max_adverse_pct"),
        "close_after_pct": replay.get("close_after_pct"),
        "advisory_replay_json": payload,
        "direction_correct_60m": direction_correct_60m,
        "pick_outcome_bucket": bucket,
    }
    # Backfill session_window and regime_at_pick from signal_json if not already set.
    if not signal.get("session_window"):
        sj = signal.get("signal_json") or {}
        sw = sj.get("session_window") if isinstance(sj, dict) else None
        if sw:
            result["session_window"] = sw
    if not signal.get("regime_at_pick"):
        mc = signal.get("market_context_json") or {}
        regime = mc.get("regime") if isinstance(mc, dict) else None
        if regime:
            result["regime_at_pick"] = regime
    return result


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
            # Mark as permanently failed so this signal stops being retried
            # every cycle. Writes forward_scored_at with an error payload so
            # get_unscored_advisory_signals() won't pick it up again.
            now_utc = datetime.now(timezone.utc)
            try:
                update_advisory_signal_replay(signal.get("id"), {
                    "forward_scored_at": now_utc.isoformat(),
                    "advisory_replay_json": {
                        "status": "error",
                        "error": str(e)[:200],
                        "symbol": signal.get("data_symbol"),
                        "failed_at": now_utc.isoformat(),
                    },
                })
            except Exception:
                pass
    if checked:
        log_event("INFO", "advisory_replay_complete", {
            "checked": checked,
            "updated": updated,
            "finalized": finalized,
            "min_age_minutes": min_age,
            "max_age_days": max_age_days,
        })


def _score_5d_return(signal: dict, created_at: datetime) -> Optional[dict]:
    """Fetch the 5-day forward return for a single advisory signal.

    Downloads 1d bars (5d period) so we only need one yfinance call per signal.
    Returns a dict of fields to update, or None on failure.
    """
    import yfinance as yf

    ticker = str(
        signal.get("data_symbol") or signal.get("primary_symbol") or ""
    ).upper()
    side = str(signal.get("side") or "BUY").upper()
    reference_price = _advisory_reference_price(signal)
    if not ticker or reference_price <= 0:
        return None

    now_utc = datetime.now(timezone.utc)
    # Require at least 5 calendar days elapsed before scoring.
    if now_utc < created_at + timedelta(days=5):
        return None

    try:
        signal_day = created_at.replace(hour=0, minute=0, second=0, microsecond=0)
        # Use explicit start/end so old backlog signals (>10 days ago) are not
        # mis-scored by picking bars from the wrong window. Fetch 12 calendar
        # days from signal_day; ~8–9 trading days, enough for 5 trading closes.
        fetch_start = signal_day.strftime("%Y-%m-%d")
        fetch_end = (signal_day + timedelta(days=12)).strftime("%Y-%m-%d")
        bars = yf.download(
            ticker,
            start=fetch_start,
            end=fetch_end,
            interval="1d",
            progress=False,
            auto_adjust=True,
        )
        if bars is None or bars.empty:
            return None
        if isinstance(bars.columns, pd.MultiIndex):
            bars.columns = bars.columns.get_level_values(0)
        if bars.index.tz is None:
            bars.index = bars.index.tz_localize("UTC")
        else:
            bars.index = bars.index.tz_convert("UTC")

        future_bars = bars[bars.index >= signal_day]
        if len(future_bars) < 2:
            return None

        # Close of the 5th trading day from signal_day (index 5 if available, else last).
        target_idx = min(5, len(future_bars) - 1)
        close_5d = float(_col_values(future_bars.iloc[[target_idx]], "Close")[0])
        if side == "BUY":
            fwd_5d = (close_5d - reference_price) / reference_price * 100.0
            direction_correct_5d = fwd_5d > 0
        else:
            fwd_5d = (reference_price - close_5d) / reference_price * 100.0
            direction_correct_5d = fwd_5d > 0

        # Finalize pick_outcome_bucket (authoritative version using T1 hit data too).
        target_hit = signal.get("target_hit_first")
        stop_hit = signal.get("stop_hit_first")
        if target_hit:
            bucket = "tp1_hit"
        elif stop_hit:
            bucket = "stop_hit"
        elif fwd_5d > 0:
            bucket = "expired_positive"
        else:
            bucket = "expired_negative"

        return {
            "forward_return_5d": round(fwd_5d, 4),
            "direction_correct_5d": direction_correct_5d,
            "pick_outcome_bucket": bucket,
        }
    except Exception:
        return None


def _replay_advisory_signals_5d():
    """Score T+5d forward return for signals that are already 60m-complete
    but haven't been scored for 5d yet. Runs nightly."""
    if not _env_bool("ADVISORY_REPLAY_ENABLED", True):
        return

    limit = _env_int("ADVISORY_REPLAY_5D_LIMIT", 20)
    signals = get_advisory_signals_needing_5d_score(limit=limit)
    if not signals:
        return

    checked = 0
    updated = 0
    for signal in signals:
        checked += 1
        created_at = _parse_supabase_time(signal.get("created_at"))
        if not created_at:
            continue
        try:
            patch = _score_5d_return(signal, created_at)
            if not patch:
                continue
            result = update_advisory_signal_replay(signal.get("id"), patch)
            if not result.get("error"):
                updated += 1
        except Exception as e:
            log_event("WARN", "advisory_replay_5d_failed", {
                "id": signal.get("id"),
                "symbol": signal.get("data_symbol"),
                "error": str(e)[:160],
            })
    if checked:
        log_event("INFO", "advisory_replay_5d_complete", {
            "checked": checked, "updated": updated,
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
