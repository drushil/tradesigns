"""
backend/advisory_auto/replay.py
Offline replay harness for the advisory-auto exit logic.

Re-runs the *shared* exit scanner (_scan_bars_for_exit, the same code the live
simulator uses) over historical 1m bars with configurable near-T1 arm / retrace
parameters, so give-back exit thresholds get chosen from data instead of guessed.

For each parameter combo it reports, per policy:
  - win / loss counts and net avg R
  - converts:    sims that were a loss at baseline but a win at this combo
  - runner_cost: sims whose R *fell* vs baseline (winners capped early)

Baseline = the current production defaults (arm 0.8, retrace 0.5R).

Limitation: yfinance 1m history is ~7 days, so only recent sims are replayable.
Permanent reproducibility would require storing the fill->close bar path per sim.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from backend.advisory_auto.simulator import (
    _scan_bars_for_exit,
    _fetch_1m_bars,
    _yfinance_symbol,
    _parse_dt,
    _us_session_close_utc,
    _r_multiple,
)


def _bars_from_json(bars_json: dict):
    """Rebuild a 1m OHLC DataFrame from a stored bar path (see
    simulator._capture_bar_path). Returns None if the payload is empty."""
    if not isinstance(bars_json, dict):
        return None
    rows = bars_json.get("bars") or []
    day = bars_json.get("date")
    if not rows or not day:
        return None
    idx, lows, highs, closes = [], [], [], []
    for r in rows:
        try:
            hhmm, low, high, close = r[0], float(r[1]), float(r[2]), float(r[3])
            idx.append(pd.Timestamp(f"{day} {hhmm}", tz="UTC"))
            lows.append(low); highs.append(high); closes.append(close)
        except (TypeError, ValueError, IndexError):
            continue
    if not idx:
        return None
    return pd.DataFrame({"Low": lows, "High": highs, "Close": closes},
                        index=pd.DatetimeIndex(idx))

BASELINE_ARM = 0.8
BASELINE_RETRACE = 0.5

REPLAYABLE_STATUSES = (
    "hit_stop", "hit_target_1", "hit_target_2", "hit_near_t1_protection",
    "closed_eod_win", "closed_eod_loss", "closed_eod",
)


def _exit_price(status: str, fill: float, stop: float,
                t1: Optional[float], t2: Optional[float],
                term_close: float) -> float:
    """The price the trade books at for a given terminal status — mirrors the
    live simulator's downstream exit-price derivation."""
    if status == "hit_stop":
        return stop
    if status == "hit_target_2" and t2 is not None:
        return t2
    if status == "hit_target_1" and t1 is not None:
        return t1
    # near-T1 protection and EOD close both book at the bar/close price.
    return term_close


def replay_one(sim: dict, arm_frac: float, retrace_r: float,
               bars=None) -> Optional[dict]:
    """Replay a single sim's fill->session-close path with the given params.
    Returns {status, r, mfe, mae, exit_price} or None if not replayable."""
    fill_at = _parse_dt(sim.get("fill_at"))
    if fill_at is None:
        return None
    fill = float(sim.get("fill_price") or 0)
    stop = float(sim.get("stop_price") or 0)
    if fill <= 0 or stop <= 0:
        return None
    t1 = float(sim["target_1"]) if sim.get("target_1") is not None else None
    t2 = float(sim["target_2"]) if sim.get("target_2") is not None else None
    session_close = _us_session_close_utc(fill_at) or (fill_at + timedelta(hours=7))
    if bars is None:
        # Prefer the stored bar path (survives yfinance's ~1-2 day 1m window);
        # fall back to a live re-fetch for very recent sims not yet captured.
        bars = _bars_from_json(sim.get("bars_json"))
        if bars is None:
            bars = _fetch_1m_bars(_yfinance_symbol(sim.get("data_symbol")), fill_at, session_close)
    if bars is None or bars.empty:
        return None

    status, term_close, _ts, mfe, mae, _peak = _scan_bars_for_exit(
        bars, fill, stop, t1, t2, 0.0, 0.0, arm_frac, retrace_r
    )
    if status is None:
        # Survived to session close — mark to the last close (EOD).
        eod_price = term_close or fill
        status = "closed_eod_win" if eod_price > fill else "closed_eod_loss"
        exit_px = eod_price
    else:
        exit_px = _exit_price(status, fill, stop, t1, t2, term_close or fill)

    r = _r_multiple(fill, stop, exit_px, str(sim.get("side") or "BUY"))
    return {"status": status, "r": r, "mfe": round(mfe, 3),
            "mae": round(mae, 3), "exit_price": round(exit_px, 4)}


def _is_win(status: str) -> bool:
    return status in ("hit_target_1", "hit_target_2",
                      "hit_near_t1_protection", "closed_eod_win")


def sweep(sims: list, arms: list, retraces: list) -> list:
    """Replay every sim across the arm x retrace grid. Bars are fetched once per
    sim and reused. Returns a list of per-(arm, retrace, policy) report rows."""
    bar_cache: dict = {}

    def bars_for(sim):
        key = (sim.get("data_symbol"), sim.get("fill_at"))
        if key not in bar_cache:
            bars = _bars_from_json(sim.get("bars_json"))
            if bars is None:
                fill_at = _parse_dt(sim.get("fill_at"))
                session_close = _us_session_close_utc(fill_at) if fill_at else None
                bars = _fetch_1m_bars(
                    _yfinance_symbol(sim.get("data_symbol")), fill_at, session_close
                ) if fill_at and session_close else None
            bar_cache[key] = bars
        return bar_cache[key]

    # Baseline outcome per sim (current production defaults).
    baseline: dict = {}
    for sim in sims:
        baseline[sim["id"]] = replay_one(sim, BASELINE_ARM, BASELINE_RETRACE, bars_for(sim))

    rows = []
    for arm in arms:
        for rr in retraces:
            per_policy: dict = defaultdict(
                lambda: {"n": 0, "wins": 0, "sum_r": 0.0, "converts": 0, "runner_cost_r": 0.0}
            )
            for sim in sims:
                res = replay_one(sim, arm, rr, bars_for(sim))
                base = baseline.get(sim["id"])
                if res is None or res["r"] is None:
                    continue
                policy = sim.get("entry_policy") or sim.get("mode") or "unknown"
                agg = per_policy[policy]
                agg["n"] += 1
                agg["sum_r"] += res["r"]
                if _is_win(res["status"]):
                    agg["wins"] += 1
                if base and base.get("r") is not None:
                    if base["r"] < 0 and res["r"] > 0:
                        agg["converts"] += 1
                    if res["r"] < base["r"]:
                        agg["runner_cost_r"] += (base["r"] - res["r"])
            for policy, agg in sorted(per_policy.items()):
                n = agg["n"]
                rows.append({
                    "arm": arm, "retrace_r": rr, "policy": policy,
                    "n": n, "wins": agg["wins"], "losses": n - agg["wins"],
                    "avg_r": round(agg["sum_r"] / n, 3) if n else None,
                    "converts_vs_base": agg["converts"],
                    "runner_cost_r": round(agg["runner_cost_r"], 3),
                })
    return rows


def load_replayable_sims(market: str = "US", days_back: int = 7, limit: int = 500) -> list:
    """Load recently-resolved BUY sims that can still be replayed (within the
    yfinance 1m window). Requires DB credentials — used in deployed runs."""
    from datetime import datetime, timezone
    from database.client import get_client
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    db = get_client()
    res = (db.table("advisory_auto_simulations")
           .select("id,data_symbol,market,side,grade,mode,entry_policy,status,"
                   "fill_at,fill_price,stop_price,target_1,target_2,closed_at,bars_json")
           .eq("market", market.upper())
           .eq("side", "BUY")
           .in_("status", list(REPLAYABLE_STATUSES))
           .gte("fill_at", cutoff)
           .order("fill_at", desc=True)
           .limit(limit)
           .execute())
    return res.data or []


def main():
    import json
    sims = load_replayable_sims()
    rows = sweep(sims, arms=[0.5, 0.6, 0.7, 0.8], retraces=[0.3, 0.4, 0.5])
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
