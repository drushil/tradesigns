"""
backend/advisory_auto/simulator.py
Watch-limit simulator for advisory-auto dry-run measurement.

For each live BUY watch advisory_signal with valid entry/stop/target levels,
this module records what a limit order at the entry band WOULD have done.
The goal is to measure advisory edge from the way trades actually happen
(watch → pullback fill → managed exit), not only the rare same-bar trade-
stage path the strict auto-allocator evaluates.

This is measurement-only. No broker orders are submitted. The sim runs on
yfinance 1-minute bars between successive cycles. Per-bar transitions:

  pending  → filled        if any bar's [low, high] intersects [entry_min, entry_max]
  pending  → expired       if valid_until passed without a fill
  filled   → hit_stop      if bar.low <= stop_price
  filled   → hit_target_2  if bar.high >= target_2 (regardless of T1)
  filled   → hit_target_1  if bar.high >= target_1 and not hit_target_2 yet

Stop wins ties (pessimistic risk assumption when stop and target intersect
the same bar). MFE/MAE are tracked separately so a hit_target_1 row still
shows the maximum favorable excursion — useful for "would I have held to T2".

The simulator wakes once per advisory cycle. Out-of-window cycles short-
circuit so the table stays accurate across timezones.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from database.client import (
    get_advisory_auto_chase_skips,
    get_advisory_auto_sim_signal_ids,
    get_active_advisory_auto_simulations,
    get_eligible_advisory_signals_for_simulation,
    get_latest_advisory_signal_for_symbol,
    get_open_filled_simulations,
    insert_advisory_auto_simulation,
    log_event,
    update_advisory_auto_simulation,
)


# Sim creation window — pick up any BUY watch from the last 30 min that
# doesn't have a sim row yet. Wider than the auto-allocator's 6-min
# freshness because measurement isn't time-sensitive.
SIM_CREATION_LOOKBACK_MIN = int(os.getenv("ADVISORY_AUTO_SIM_LOOKBACK_MIN", "30"))

# After this many hours since fill with no terminal, force-close as expired.
SIM_MAX_FILL_HOURS = int(os.getenv("ADVISORY_AUTO_SIM_MAX_FILL_HOURS", "24"))

# Skip processing entirely if disabled.
SIM_ENABLED = os.getenv("ADVISORY_AUTO_SIM_ENABLED", "true").strip().lower() != "false"

# Pending-cancel: drop a resting watch-limit sim if the symbol's latest live
# signal has turned bearish or its grade has fallen this many tiers or more.
SIM_CANCEL_GRADE_DROP = int(os.getenv("ADVISORY_AUTO_SIM_CANCEL_GRADE_DROP", "2"))

# Near-T1 protection: arm once MFE reaches this fraction of the fill→T1 distance,
# then book if price retraces this many R (R = fill - stop) back from the peak.
SIM_NEAR_T1_ARM_FRAC = float(os.getenv("ADVISORY_AUTO_SIM_NEAR_T1_ARM_FRAC", "0.8"))
SIM_NEAR_T1_RETRACE_R = float(os.getenv("ADVISORY_AUTO_SIM_NEAR_T1_RETRACE_R", "0.5"))

# Momentum-continuation simulator: for strong watch signals, measure the
# alternative "buy the signal price" policy next to the normal pullback limit.
SIM_MOMENTUM_ENABLED = (
    os.getenv("ADVISORY_AUTO_SIM_MOMENTUM_ENABLED", "true").strip().lower() != "false"
)
SIM_MOMENTUM_MIN_GRADE = os.getenv("ADVISORY_AUTO_SIM_MOMENTUM_MIN_GRADE", "A")

_GRADE_RANK = {"A+": 4, "A": 3, "B": 2, "C": 1}


def _grade_rank(grade) -> int:
    return _GRADE_RANK.get(str(grade or "").strip().upper(), 0)


# Current simulator version. Bump when logic changes materially so old rows
# don't contaminate new learning queries.
SIM_VERSION = 2

_STATUS_TO_CLOSURE_REASON = {
    "hit_target_1":          "target_1",
    "hit_target_2":          "target_2",
    "hit_stop":              "stop",
    "hit_near_t1_protection": "near_t1_protection",
    "expired":               "expired_pending",
    "cancelled_signal_weak": "cancelled_weak",
    "closed_eod":            "eod_close",
    "closed_eod_win":        "eod_close",
    "closed_eod_loss":       "eod_close",
}


def _closure_reason(status: str) -> Optional[str]:
    return _STATUS_TO_CLOSURE_REASON.get(str(status or ""))


def _r_multiple(fill_price: float, stop_price: float,
                exit_price: float, side: str = "BUY") -> Optional[float]:
    """Return signed R-multiple: (exit - fill) / risk, where risk = |fill - stop|."""
    try:
        risk = abs(fill_price - stop_price)
        if risk <= 0:
            return None
        pnl = (exit_price - fill_price) if side.upper() == "BUY" else (fill_price - exit_price)
        return round(pnl / risk, 4)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _entry_policy_quality(fill_price: float,
                          entry_min: float, entry_max: float) -> Optional[float]:
    """0 = filled at entry_min, 1 = at entry_max, >1 = chased above band."""
    try:
        band = entry_max - entry_min
        if band <= 0:
            return None
        return round((fill_price - entry_min) / band, 3)
    except (TypeError, ValueError):
        return None


def _float_or_none(value) -> Optional[float]:
    try:
        if value is None:
            return None
        val = float(value)
        return val if val > 0 else None
    except (TypeError, ValueError):
        return None


def _signal_reference_price(sig: dict) -> Optional[float]:
    """Best available price at the time the advisory card was generated."""
    for key in ("reference_price", "price_native", "last_price"):
        val = _float_or_none(sig.get(key))
        if val:
            return val

    quality = sig.get("data_quality_json") or {}
    if isinstance(quality, dict):
        val = _float_or_none(quality.get("last_price"))
        if val:
            return val

    sj = sig.get("signal_json") or {}
    if isinstance(sj, dict):
        for key in ("reference_price", "price_native", "last_price", "current_price", "price"):
            val = _float_or_none(sj.get(key))
            if val:
                return val
        atr = sj.get("atr_data") or {}
        if isinstance(atr, dict):
            val = _float_or_none(atr.get("current_price"))
            if val:
                return val

    return None


def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _iso_z(dt: datetime) -> str:
    """Serialize a tz-aware datetime as a UTC ISO string with a trailing Z.

    datetime.now(timezone.utc).isoformat() yields "...+00:00", and Postgres
    rejects "...+00:00Z". Strip the tzinfo first so the suffix is clean.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat() + "Z"


def _fetch_1m_bars(symbol: str, start: datetime, end: datetime) -> Optional[pd.DataFrame]:
    """Fetch 1-minute bars covering [start, end] in UTC. Returns None on failure."""
    if end <= start:
        return None
    try:
        import yfinance as yf
        period_minutes = (end - start).total_seconds() / 60.0
        # yfinance 1m bars limited to ~7 days; pick the smallest period that covers.
        if period_minutes <= 60 * 6:
            period = "1d"
        elif period_minutes <= 60 * 24 * 2:
            period = "2d"
        else:
            period = "5d"
        df = yf.download(
            symbol,
            period=period,
            interval="1m",
            prepost=False,
            progress=False,
            auto_adjust=True,
        )
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index, utc=True, errors="coerce")
        if df.index.tz is None:
            df.index = df.index.tz_localize(timezone.utc)
        else:
            df.index = df.index.tz_convert(timezone.utc)
        return df[(df.index >= start) & (df.index <= end)]
    except Exception:
        return None


def _create_pending_sims(market: str = "US") -> int:
    """Create simulation rows for new live BUY watch advisory_signals."""
    eligible = get_eligible_advisory_signals_for_simulation(
        market=market, max_age_minutes=SIM_CREATION_LOOKBACK_MIN, limit=200
    )
    if not eligible:
        return 0
    existing = get_advisory_auto_sim_signal_ids()
    created = 0
    for sig in eligible:
        sig_id = int(sig.get("id") or 0)
        if not sig_id:
            continue
        # Only watch + trade stages are interesting. Ignition is too early.
        sj = sig.get("signal_json") or {}
        stage = str(sj.get("alert_stage") or "").lower() if isinstance(sj, dict) else ""
        if stage not in ("watch", "trade"):
            continue
        # trade-stage = "enter now" limit; watch-stage = "wait for the pullback".
        mode = "trade_now" if stage == "trade" else "watch_pullback"
        if (sig_id, mode) in existing:
            continue
        entry_min = float(sig.get("entry_min") or 0)
        entry_max = float(sig.get("entry_max") or 0)
        stop_price = float(sig.get("stop_price") or 0)
        if not (entry_min and entry_max and stop_price):
            continue
        composite = sig.get("composite_score")
        breakout = sig.get("breakout_quality")
        payload = {
            "advisory_signal_id": sig_id,
            "data_symbol": sig.get("data_symbol"),
            "market": sig.get("market"),
            "side": sig.get("side") or "BUY",
            "grade": sig.get("grade"),
            "alert_stage": stage,
            "mode": mode,
            "composite_score": float(composite) if composite is not None else None,
            "breakout_quality": float(breakout) if breakout is not None else None,
            "currency": sig.get("currency"),
            "entry_min": entry_min,
            "entry_max": entry_max,
            "stop_price": stop_price,
            "target_1": float(sig.get("target_1")) if sig.get("target_1") is not None else None,
            "target_2": float(sig.get("target_2")) if sig.get("target_2") is not None else None,
            "suggested_size_eur": sig.get("suggested_size_eur"),
            # Anchor simulated_at to the signal's created_at so the fill check
            # window matches the period the limit order would actually have
            # been live (signal creation → valid_until). Real-time runs and
            # backfill runs use the same code path; the only difference is
            # how many bars have elapsed when the first cycle picks it up.
            "simulated_at": sig.get("created_at"),
            "valid_until": sig.get("valid_until"),
            "status": "pending",
            "entry_policy": mode,
            "sim_version": SIM_VERSION,
        }
        result = insert_advisory_auto_simulation(payload)
        if "error" not in result:
            created += 1
            existing.add((sig_id, mode))
    return created


def _parse_chase_price(reason) -> Optional[float]:
    """Pull the chased price out of an executor skip reason.

    Format: "skipped_chase:{current:.2f}>{do_not_chase:.2f}". We want the price
    the executor saw when it refused to chase — the value before the '>'.
    """
    try:
        after = str(reason).split(":", 1)[1]
        cur = after.split(">", 1)[0]
        return float(cur)
    except Exception:
        return None


def _create_chase_tracker_sims(market: str = "US") -> int:
    """Create filled chase_tracker sims for BUY signals the executor refused to
    chase. The synthetic fill is the chased price itself, so _process_filled
    measures what buying above the do-not-chase line would have done."""
    skips = get_advisory_auto_chase_skips(
        market=market, max_age_minutes=SIM_CREATION_LOOKBACK_MIN, limit=200
    )
    if not skips:
        return 0
    existing = get_advisory_auto_sim_signal_ids()
    created = 0
    for sig in skips:
        sig_id = int(sig.get("id") or 0)
        if not sig_id or (sig_id, "chase_tracker") in existing:
            continue
        chase_price = _parse_chase_price(sig.get("auto_skip_reason"))
        stop_price = float(sig.get("stop_price") or 0)
        entry_min = float(sig.get("entry_min") or 0)
        entry_max = float(sig.get("entry_max") or 0)
        if not (chase_price and stop_price and chase_price > stop_price):
            continue
        fill_at = sig.get("auto_checked_at") or sig.get("created_at")
        composite = sig.get("composite_score")
        breakout = sig.get("breakout_quality")
        sj = sig.get("signal_json") or {}
        stage = str(sj.get("alert_stage") or "trade").lower() if isinstance(sj, dict) else "trade"
        payload = {
            "advisory_signal_id": sig_id,
            "data_symbol": sig.get("data_symbol"),
            "market": sig.get("market"),
            "side": sig.get("side") or "BUY",
            "grade": sig.get("grade"),
            "alert_stage": stage,
            "mode": "chase_tracker",
            "composite_score": float(composite) if composite is not None else None,
            "breakout_quality": float(breakout) if breakout is not None else None,
            "currency": sig.get("currency"),
            "entry_min": entry_min or chase_price,
            "entry_max": entry_max or chase_price,
            "stop_price": stop_price,
            "target_1": float(sig.get("target_1")) if sig.get("target_1") is not None else None,
            "target_2": float(sig.get("target_2")) if sig.get("target_2") is not None else None,
            "suggested_size_eur": sig.get("suggested_size_eur"),
            "simulated_at": fill_at,
            "valid_until": sig.get("valid_until"),
            # Already filled at the chased price — no limit to rest, we paid up.
            "status": "filled",
            "fill_at": fill_at,
            "fill_price": round(chase_price, 4),
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "notes": {"synthetic_fill": "chase_price"},
            "entry_policy": "chase_tracker",
            "entry_policy_quality": _entry_policy_quality(
                chase_price, entry_min or chase_price, entry_max or chase_price
            ),
            "sim_version": SIM_VERSION,
        }
        result = insert_advisory_auto_simulation(payload)
        if "error" not in result:
            created += 1
            existing.add((sig_id, "chase_tracker"))
    return created


def _create_momentum_continuation_sims(market: str = "US") -> int:
    """Create filled sims for strong watch signals that never reach trade stage.

    This is intentionally independent of the executor's strict "trade" stage:
    A/A+ watches often move without revisiting the pullback band, so this policy
    measures buying the advisory reference price head-to-head against
    watch_pullback rather than assuming "no trade" means "no edge".
    """
    if not SIM_MOMENTUM_ENABLED:
        return 0
    eligible = get_eligible_advisory_signals_for_simulation(
        market=market, max_age_minutes=SIM_CREATION_LOOKBACK_MIN, limit=200
    )
    if not eligible:
        return 0

    min_rank = _grade_rank(SIM_MOMENTUM_MIN_GRADE)
    existing = get_advisory_auto_sim_signal_ids()
    created = 0
    for sig in eligible:
        sig_id = int(sig.get("id") or 0)
        if not sig_id or (sig_id, "momentum_continuation") in existing:
            continue
        if _grade_rank(sig.get("grade")) < min_rank:
            continue

        sj = sig.get("signal_json") or {}
        stage = str(sj.get("alert_stage") or "").lower() if isinstance(sj, dict) else ""
        if stage != "watch":
            continue

        fill_price = _signal_reference_price(sig)
        entry_min = float(sig.get("entry_min") or 0)
        entry_max = float(sig.get("entry_max") or 0)
        stop_price = float(sig.get("stop_price") or 0)
        side = str(sig.get("side") or "BUY").upper()
        if not (fill_price and stop_price and entry_min and entry_max):
            continue
        if side == "BUY" and fill_price <= stop_price:
            continue
        if side == "SELL" and fill_price >= stop_price:
            continue

        composite = sig.get("composite_score")
        breakout = sig.get("breakout_quality")
        fill_at = sig.get("created_at")
        payload = {
            "advisory_signal_id": sig_id,
            "data_symbol": sig.get("data_symbol"),
            "market": sig.get("market"),
            "side": side,
            "grade": sig.get("grade"),
            "alert_stage": stage,
            "mode": "momentum_continuation",
            "composite_score": float(composite) if composite is not None else None,
            "breakout_quality": float(breakout) if breakout is not None else None,
            "currency": sig.get("currency"),
            "entry_min": entry_min,
            "entry_max": entry_max,
            "stop_price": stop_price,
            "target_1": float(sig.get("target_1")) if sig.get("target_1") is not None else None,
            "target_2": float(sig.get("target_2")) if sig.get("target_2") is not None else None,
            "suggested_size_eur": sig.get("suggested_size_eur"),
            "simulated_at": fill_at,
            "valid_until": sig.get("valid_until"),
            "status": "filled",
            "fill_at": fill_at,
            "fill_price": round(float(fill_price), 4),
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "notes": {"synthetic_fill": "signal_reference_price"},
            "entry_policy": "momentum_continuation",
            "entry_policy_quality": _entry_policy_quality(fill_price, entry_min, entry_max),
            "sim_version": SIM_VERSION,
        }
        result = insert_advisory_auto_simulation(payload)
        if "error" not in result:
            created += 1
            existing.add((sig_id, "momentum_continuation"))
    return created


def _signal_weakened(sim: dict) -> Optional[str]:
    """Return a short reason if the symbol's latest live signal no longer
    supports the resting BUY limit (flipped bearish / grade collapsed), else None."""
    latest = get_latest_advisory_signal_for_symbol(
        str(sim.get("data_symbol") or ""), market=str(sim.get("market") or "US")
    )
    if not latest:
        return None
    # Don't react to the very signal that spawned this sim.
    if int(latest.get("id") or 0) == int(sim.get("advisory_signal_id") or 0):
        return None
    if str(latest.get("side") or "BUY").upper() == "SELL":
        return "latest_signal_sell"
    comp = latest.get("composite_score")
    if comp is not None and float(comp) < 0:
        return "latest_composite_bearish"
    drop = _grade_rank(sim.get("grade")) - _grade_rank(latest.get("grade"))
    if _grade_rank(sim.get("grade")) > 0 and drop >= SIM_CANCEL_GRADE_DROP:
        return f"grade_drop:{sim.get('grade')}->{latest.get('grade')}"
    return None


def _yfinance_symbol(market_symbol: str) -> str:
    """Pass-through for US tickers. (EU tickers carry suffixes that yfinance
    already understands.) Kept as a hook for future market-specific quirks."""
    return market_symbol


def _process_pending(sim: dict, now_utc: datetime) -> dict:
    """Check whether a pending sim filled. Returns the update payload.

    The bar window is [last_check, min(now, valid_until)]. We check for a fill
    in that window first; only if no fill is found and now is past valid_until
    do we mark expired. This keeps backfill/late-create runs honest — a sim
    born after valid_until still gets a proper fill check on historical bars
    before being closed.
    """
    # Pull the resting limit if conviction has died since it was placed. We'd
    # have cancelled the order rather than let it fill into a souring setup, so
    # this terminates before the fill check (slight imprecision on exact
    # fill-vs-cancel timing is acceptable for measurement).
    weak_reason = _signal_weakened(sim)
    if weak_reason:
        return {
            "status": "cancelled_signal_weak",
            "last_checked_at": _iso_z(now_utc),
            "closed_at": _iso_z(now_utc),
            "notes": {**(sim.get("notes") or {}), "cancel_reason": weak_reason},
        }
    valid_until = _parse_dt(sim.get("valid_until"))
    simulated_at = _parse_dt(sim.get("simulated_at")) or now_utc
    last_check = _parse_dt(sim.get("last_checked_at")) or simulated_at
    window_end = min(now_utc, valid_until) if valid_until else now_utc
    bars = _fetch_1m_bars(_yfinance_symbol(sim["data_symbol"]), last_check, window_end)
    if bars is None or bars.empty:
        if valid_until and now_utc > valid_until:
            return {
                "status": "expired",
                "last_checked_at": _iso_z(now_utc),
                "closed_at": _iso_z(now_utc),
                "notes": {**(sim.get("notes") or {}), "expired_reason": "no_bars_in_window"},
            }
        return {"last_checked_at": _iso_z(now_utc)}
    entry_min = float(sim["entry_min"])
    entry_max = float(sim["entry_max"])
    for ts, bar in bars.iterrows():
        bar_low = float(bar.get("Low") or 0)
        bar_high = float(bar.get("High") or 0)
        if bar_low <= 0 or bar_high <= 0:
            continue
        # Limit fill heuristic: the bar's range must intersect the entry band.
        if bar_low <= entry_max and bar_high >= entry_min:
            # Fill price = the closer band edge to the bar's open / midpoint.
            # For BUY limit, we'd want to buy as cheaply as possible — fill at
            # entry_min if the bar dipped that far, else at the midpoint of
            # the intersection.
            if bar_low <= entry_min:
                fill = entry_min
            elif bar_high >= entry_max:
                fill = entry_max
            else:
                fill = (max(bar_low, entry_min) + min(bar_high, entry_max)) / 2.0
            epq = _entry_policy_quality(fill, entry_min, entry_max)
            return {
                "status": "filled",
                "fill_at": _iso_z(ts.to_pydatetime()),
                "fill_price": round(fill, 4),
                "mfe_pct": 0.0,
                "mae_pct": 0.0,
                "last_price": round(float(bar.get("Close") or fill), 4),
                "last_checked_at": _iso_z(now_utc),
                "entry_policy_quality": epq,
            }
    # No fill in this window. Mark expired if we're past valid_until.
    if valid_until and now_utc > valid_until:
        return {
            "status": "expired",
            "last_checked_at": _iso_z(now_utc),
            "closed_at": _iso_z(now_utc),
            "last_price": round(float(bars["Close"].iloc[-1]), 4) if "Close" in bars else None,
            "notes": {**(sim.get("notes") or {}), "expired_reason": "no_fill_before_valid_until"},
        }
    return {
        "last_checked_at": _iso_z(now_utc),
        "last_price": round(float(bars["Close"].iloc[-1]), 4) if "Close" in bars else None,
    }


def _process_filled(sim: dict, now_utc: datetime) -> dict:
    """Track MFE/MAE and detect terminal hits for a filled sim."""
    fill_at = _parse_dt(sim.get("fill_at")) or now_utc
    last_check = _parse_dt(sim.get("last_checked_at")) or fill_at
    fill_price = float(sim.get("fill_price") or 0)
    stop_price = float(sim.get("stop_price") or 0)
    t1 = float(sim["target_1"]) if sim.get("target_1") is not None else None
    t2 = float(sim["target_2"]) if sim.get("target_2") is not None else None
    if fill_price <= 0 or stop_price <= 0:
        # Malformed sim — close it as expired.
        return {
            "status": "expired",
            "last_checked_at": _iso_z(now_utc),
            "closed_at": _iso_z(now_utc),
            "notes": {**(sim.get("notes") or {}), "expired_reason": "missing_levels_after_fill"},
        }
    # Force-close very stale fills so the active queue stays bounded.
    if fill_at and now_utc - fill_at > timedelta(hours=SIM_MAX_FILL_HOURS):
        return {
            "status": "expired",
            "last_checked_at": _iso_z(now_utc),
            "closed_at": _iso_z(now_utc),
            "notes": {**(sim.get("notes") or {}), "expired_reason": "max_fill_hours"},
        }
    bars = _fetch_1m_bars(_yfinance_symbol(sim["data_symbol"]), last_check, now_utc)
    if bars is None or bars.empty:
        return {"last_checked_at": _iso_z(now_utc)}
    mfe_pct = float(sim.get("mfe_pct") or 0)
    mae_pct = float(sim.get("mae_pct") or 0)
    for ts, bar in bars.iterrows():
        bar_low = float(bar.get("Low") or 0)
        bar_high = float(bar.get("High") or 0)
        if bar_low <= 0 or bar_high <= 0:
            continue
        # Running MFE / MAE (signed % from fill, BUY direction).
        bar_mfe = (bar_high - fill_price) / fill_price * 100.0
        bar_mae = (bar_low - fill_price) / fill_price * 100.0
        if bar_mfe > mfe_pct:
            mfe_pct = bar_mfe
        if bar_mae < mae_pct:
            mae_pct = bar_mae
        # Terminal checks, in pessimistic order: stop wins ties.
        if bar_low <= stop_price:
            return {
                "status": "hit_stop",
                "last_price": round(float(bar.get("Close") or 0), 4),
                "mfe_pct": round(mfe_pct, 4),
                "mae_pct": round(mae_pct, 4),
                "last_checked_at": _iso_z(now_utc),
                "closed_at": _iso_z(ts.to_pydatetime()),
            }
        if t2 is not None and bar_high >= t2:
            return {
                "status": "hit_target_2",
                "last_price": round(float(bar.get("Close") or 0), 4),
                "mfe_pct": round(mfe_pct, 4),
                "mae_pct": round(mae_pct, 4),
                "last_checked_at": _iso_z(now_utc),
                "closed_at": _iso_z(ts.to_pydatetime()),
            }
        if t1 is not None and bar_high >= t1:
            return {
                "status": "hit_target_1",
                "last_price": round(float(bar.get("Close") or 0), 4),
                "mfe_pct": round(mfe_pct, 4),
                "mae_pct": round(mae_pct, 4),
                "last_checked_at": _iso_z(now_utc),
                "closed_at": _iso_z(ts.to_pydatetime()),
            }
        # Near-T1 protection (lowest priority — T1/T2 take it first). Once the
        # run-up has covered ARM_FRAC of the fill→T1 distance, book the trade if
        # price gives back RETRACE_R of risk (R = fill - stop) from the peak.
        # mfe_pct already encodes the peak, so peak = fill * (1 + mfe_pct/100).
        if t1 is not None and t1 > fill_price:
            arm_pct = SIM_NEAR_T1_ARM_FRAC * (t1 - fill_price) / fill_price * 100.0
            if mfe_pct >= arm_pct:
                peak_price = fill_price * (1.0 + mfe_pct / 100.0)
                retrace_price = peak_price - SIM_NEAR_T1_RETRACE_R * (fill_price - stop_price)
                if bar_low <= retrace_price:
                    return {
                        "status": "hit_near_t1_protection",
                        "last_price": round(float(bar.get("Close") or 0), 4),
                        "mfe_pct": round(mfe_pct, 4),
                        "mae_pct": round(mae_pct, 4),
                        "last_checked_at": _iso_z(now_utc),
                        "closed_at": _iso_z(ts.to_pydatetime()),
                        "notes": {
                            **(sim.get("notes") or {}),
                            "near_t1_peak_price": round(peak_price, 4),
                        },
                    }
    return {
        "mfe_pct": round(mfe_pct, 4),
        "mae_pct": round(mae_pct, 4),
        "last_price": round(float(bars["Close"].iloc[-1]), 4) if "Close" in bars else None,
        "last_checked_at": _iso_z(now_utc),
    }


def run_advisory_auto_simulation_cycle(market: str = "US") -> dict:
    """Run one simulator cycle. Idempotent: cap-blocked watches are skipped
    because they never reach advisory_signals, and existing sims are not
    re-created.

    EOD mark-to-close runs automatically at the end of this cycle whenever
    the US session close has passed (≥30 min buffer). This ensures the last
    cycle of the day always leaves a clean scoreboard with no dangling fills.
    """
    if not SIM_ENABLED:
        return {"ran": False, "reason": "disabled"}
    now_utc = datetime.now(timezone.utc)
    created = _create_pending_sims(market=market)
    created_chase = _create_chase_tracker_sims(market=market)
    created_momentum = _create_momentum_continuation_sims(market=market)
    active = get_active_advisory_auto_simulations(limit=200)
    transitions: dict = {
        "pending_checked": 0,
        "filled_new": 0,
        "filled_checked": 0,
        "expired": 0,
        "cancelled_signal_weak": 0,
        "hit_stop": 0,
        "hit_target_1": 0,
        "hit_target_2": 0,
        "hit_near_t1_protection": 0,
        "errors": 0,
    }
    for sim in active:
        try:
            status = str(sim.get("status") or "")
            if status == "pending":
                transitions["pending_checked"] += 1
                update = _process_pending(sim, now_utc)
                new_status = update.get("status") or status
                if new_status == "filled":
                    transitions["filled_new"] += 1
                elif new_status in ("expired", "cancelled_signal_weak"):
                    transitions[new_status] += 1
                    cr = _closure_reason(new_status)
                    if cr and not update.get("closure_reason"):
                        update["closure_reason"] = cr
            elif status == "filled":
                transitions["filled_checked"] += 1
                update = _process_filled(sim, now_utc)
                new_status = update.get("status") or status
                if new_status in ("hit_stop", "hit_target_1", "hit_target_2",
                                  "hit_near_t1_protection", "expired"):
                    transitions[new_status] += 1
                    # Enrich terminal updates with learning columns.
                    cr = _closure_reason(new_status)
                    if cr and not update.get("closure_reason"):
                        update["closure_reason"] = cr
                    fill_price = float(sim.get("fill_price") or 0)
                    stop_price = float(sim.get("stop_price") or 0)
                    # Use the actual terminal level, not bar close, so r_multiple
                    # reflects what you'd actually get: stop_price for a stopped-out
                    # trade, target_1/target_2 for a target hit, last_price only as
                    # a fallback for near_t1 or expired (no clean terminal level).
                    if new_status == "hit_stop":
                        exit_price = stop_price
                    elif new_status == "hit_target_2":
                        exit_price = float(sim.get("target_2") or update.get("last_price") or 0)
                    elif new_status == "hit_target_1":
                        exit_price = float(sim.get("target_1") or update.get("last_price") or 0)
                    else:
                        exit_price = float(update.get("last_price") or 0)
                    if fill_price > 0 and stop_price > 0 and exit_price > 0:
                        r = _r_multiple(fill_price, stop_price, exit_price,
                                        side=str(sim.get("side") or "BUY"))
                        if r is not None and not sim.get("r_multiple"):
                            update["r_multiple"] = r
            else:
                continue
            update_advisory_auto_simulation(int(sim["id"]), update)
        except Exception as e:
            transitions["errors"] += 1
            log_event("WARN", "advisory_auto_sim_error", {
                "sim_id": sim.get("id"),
                "symbol": sim.get("data_symbol"),
                "error": str(e)[:160],
            })
    # ── EOD mark-to-close: runs automatically when session is past close ─────
    eod_closed = _eod_close_fills(market=market, now_utc=now_utc)

    log_event("INFO", "advisory_auto_sim_cycle", {
        "market": market,
        "created": created,
        "created_chase": created_chase,
        "created_momentum": created_momentum,
        "active": len(active),
        "eod_closed": eod_closed,
        **transitions,
    })
    return {
        "ran": True,
        "market": market,
        "created": created,
        "created_chase": created_chase,
        "created_momentum": created_momentum,
        "active": len(active),
        "eod_closed": eod_closed,
        **transitions,
    }


# ── EOD mark-to-close ────────────────────────────────────────────────────────

def _us_session_close_utc(fill_at: datetime) -> Optional[datetime]:
    """Return the UTC datetime of the US session close (16:00 ET) for the
    calendar date the fill belongs to. Returns None if timezone data is
    unavailable (caller falls back to a UTC approximation)."""
    try:
        from zoneinfo import ZoneInfo
        ny_tz = ZoneInfo("America/New_York")
        fill_ny = fill_at.astimezone(ny_tz)
        close_ny = fill_ny.replace(hour=16, minute=0, second=0, microsecond=0)
        return close_ny.astimezone(timezone.utc)
    except Exception:
        # Fallback: assume 20:00 UTC (EDT offset); not perfect in winter but
        # close enough — the 30-min buffer in the caller absorbs the error.
        return fill_at.replace(hour=20, minute=0, second=0, microsecond=0,
                               tzinfo=timezone.utc)


def _eod_close_price_fallback(sim: dict, session_close: datetime) -> Optional[float]:
    """Return the last available close at/before session close.

    _process_filled can legitimately return no last_price if yfinance's final
    slice is empty. For EOD accounting, use the latest available 1m close from
    the filled session before falling back to any previously stored last_price.
    """
    fill_at = _parse_dt(sim.get("fill_at"))
    start = fill_at or (session_close - timedelta(hours=7))
    bars = _fetch_1m_bars(str(sim.get("data_symbol") or ""), start, session_close)
    if bars is not None and not bars.empty and "Close" in bars:
        close = _float_or_none(bars["Close"].iloc[-1])
        if close:
            return close
    return _float_or_none(sim.get("last_price"))


def _eod_close_fills(market: str = "US", now_utc: Optional[datetime] = None) -> int:
    """Mark filled sims whose US session has ended as closed_eod.

    For each open fill:
      1. Determine the session-close UTC for its date (16:00 ET).
      2. If now_utc is at least 30 minutes past that close (buffer for late
         yfinance data), the session is definitively over.
      3. Re-run _process_filled up to session_close to catch any terminal
         hits in the closing bars. If none fired, status becomes closed_eod.

    closed_eod means: survived to session end with no stop or target hit.
    It is excluded from sim_target_win_pct (inconclusive) but counted in
    sim_closed_eod so the scoreboard stays honest.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    open_fills = get_open_filled_simulations(market=market, limit=200)
    if not open_fills:
        return 0
    closed = 0
    for sim in open_fills:
        fill_at = _parse_dt(sim.get("fill_at"))
        if fill_at is None:
            continue
        session_close = _us_session_close_utc(fill_at)
        if session_close is None:
            continue
        # Require a 30-min buffer after session close before marking EOD —
        # yfinance occasionally delivers late bars.
        if now_utc < session_close + timedelta(minutes=30):
            continue
        try:
            # Run _process_filled capped at session_close. Any unfired
            # terminals in the closing bars are caught here; otherwise we
            # get back an mfe/mae update with no status change.
            update = _process_filled(sim, session_close)
            new_status = update.get("status")
            fill_price = float(sim.get("fill_price") or 0)
            stop_price = float(sim.get("stop_price") or 0)
            side = str(sim.get("side") or "BUY")
            if new_status and new_status != "filled":
                # A terminal fired in the closing bars — enrich with learning cols.
                cr = _closure_reason(new_status)
                if cr and not update.get("closure_reason"):
                    update["closure_reason"] = cr
                if new_status == "hit_stop":
                    exit_price = stop_price
                elif new_status == "hit_target_2":
                    exit_price = float(sim.get("target_2") or update.get("last_price") or 0)
                elif new_status == "hit_target_1":
                    exit_price = float(sim.get("target_1") or update.get("last_price") or 0)
                else:
                    exit_price = float(update.get("last_price") or 0)
                if fill_price > 0 and stop_price > 0 and exit_price > 0 and not sim.get("r_multiple"):
                    r = _r_multiple(fill_price, stop_price, exit_price, side)
                    if r is not None:
                        update["r_multiple"] = r
                update_advisory_auto_simulation(int(sim["id"]), update)
                log_event("INFO", "advisory_auto_sim_eod_terminal", {
                    "sim_id": sim["id"], "symbol": sim.get("data_symbol"),
                    "status": new_status,
                })
            else:
                # No terminal — mark as closed_eod with win/loss status and new cols.
                eod_price = float(update.get("last_price") or 0)
                if eod_price <= 0:
                    fallback_price = _eod_close_price_fallback(sim, session_close)
                    eod_price = float(fallback_price or 0)
                    if eod_price > 0:
                        update["last_price"] = round(eod_price, 4)
                if eod_price > 0 and fill_price > 0:
                    eod_win = (eod_price > fill_price) if side == "BUY" else (eod_price < fill_price)
                    eod_status = "closed_eod_win" if eod_win else "closed_eod_loss"
                else:
                    eod_status = "closed_eod"
                eod_update = {
                    **update,
                    "status": eod_status,
                    "closed_at": _iso_z(session_close),
                    "closure_reason": "eod_close",
                    "eod_marked_at": _iso_z(now_utc),
                    "eod_close_price": round(eod_price, 4) if eod_price else None,
                }
                if fill_price > 0 and stop_price > 0 and eod_price > 0 and not sim.get("r_multiple"):
                    r = _r_multiple(fill_price, stop_price, eod_price, side)
                    if r is not None:
                        eod_update["r_multiple"] = r
                update_advisory_auto_simulation(int(sim["id"]), eod_update)
                log_event("INFO", "advisory_auto_sim_eod_close", {
                    "sim_id": sim["id"], "symbol": sim.get("data_symbol"),
                    "grade": sim.get("grade"), "mode": sim.get("mode"),
                    "status": eod_status,
                    "mfe_pct": update.get("mfe_pct"),
                    "mae_pct": update.get("mae_pct"),
                    "eod_close_price": eod_price,
                })
            closed += 1
        except Exception as e:
            log_event("WARN", "advisory_auto_sim_eod_error", {
                "sim_id": sim.get("id"), "symbol": sim.get("data_symbol"),
                "error": str(e)[:160],
            })
    return closed


def run_advisory_auto_eod_close(market: str = "US") -> dict:
    """Public entry point for the EOD mark-to-close step.

    Safe to call any time — fills whose session hasn't closed yet are skipped.
    Called automatically at the end of run_advisory_auto_simulation_cycle when
    the session is past close, and can also be triggered from a dedicated EOD
    workflow step for belt-and-suspenders coverage.
    """
    now_utc = datetime.now(timezone.utc)
    closed = _eod_close_fills(market=market, now_utc=now_utc)
    log_event("INFO", "advisory_auto_sim_eod_cycle", {
        "market": market, "closed_eod": closed,
    })
    return {"ran": True, "market": market, "closed_eod": closed}
