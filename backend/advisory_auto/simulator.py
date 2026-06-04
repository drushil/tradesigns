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
    get_advisory_auto_sim_signal_ids,
    get_active_advisory_auto_simulations,
    get_eligible_advisory_signals_for_simulation,
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
        if not sig_id or sig_id in existing:
            continue
        # Only watch + trade stages are interesting. Ignition is too early.
        sj = sig.get("signal_json") or {}
        stage = str(sj.get("alert_stage") or "").lower() if isinstance(sj, dict) else ""
        if stage not in ("watch", "trade"):
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
        }
        result = insert_advisory_auto_simulation(payload)
        if "error" not in result:
            created += 1
            existing.add(sig_id)
    return created


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
            return {
                "status": "filled",
                "fill_at": _iso_z(ts.to_pydatetime()),
                "fill_price": round(fill, 4),
                "mfe_pct": 0.0,
                "mae_pct": 0.0,
                "last_price": round(float(bar.get("Close") or fill), 4),
                "last_checked_at": _iso_z(now_utc),
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
    return {
        "mfe_pct": round(mfe_pct, 4),
        "mae_pct": round(mae_pct, 4),
        "last_price": round(float(bars["Close"].iloc[-1]), 4) if "Close" in bars else None,
        "last_checked_at": _iso_z(now_utc),
    }


def run_advisory_auto_simulation_cycle(market: str = "US") -> dict:
    """Run one simulator cycle. Idempotent: cap-blocked watches are skipped
    because they never reach advisory_signals, and existing sims are not
    re-created."""
    if not SIM_ENABLED:
        return {"ran": False, "reason": "disabled"}
    now_utc = datetime.now(timezone.utc)
    created = _create_pending_sims(market=market)
    active = get_active_advisory_auto_simulations(limit=200)
    transitions: dict = {
        "pending_checked": 0,
        "filled_new": 0,
        "filled_checked": 0,
        "expired": 0,
        "hit_stop": 0,
        "hit_target_1": 0,
        "hit_target_2": 0,
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
                elif new_status == "expired":
                    transitions["expired"] += 1
            elif status == "filled":
                transitions["filled_checked"] += 1
                update = _process_filled(sim, now_utc)
                new_status = update.get("status") or status
                if new_status in ("hit_stop", "hit_target_1", "hit_target_2", "expired"):
                    transitions[new_status if new_status != "expired" else "expired"] += 1
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
    log_event("INFO", "advisory_auto_sim_cycle", {
        "market": market,
        "created": created,
        "active": len(active),
        **transitions,
    })
    return {
        "ran": True,
        "market": market,
        "created": created,
        "active": len(active),
        **transitions,
    }
