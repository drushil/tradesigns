"""
backend/advisory_auto/german_morning.py
German-broker pre-open morning watch (Trade Republic / Scalable Capital).

Fires ~07:25 CEST, just before the German retail venues open at 07:30
(LS Exchange for TR, gettex/Baader for Scalable). ADVISORY-ONLY.

Honesty boundary: at 07:30 CEST it is ~01:30 ET — there is no live US trading
and we have no German-venue feed. The German morning price for a US name is
market-maker repricing driven by index futures + overnight news. So this is a
pre-open *watchlist*, not a live entry signal: overnight ES/NQ futures tone +
the prior US session's strongest advisory names, with limit-only / thin-liquidity
wording. Broker-neutral (TR and Scalable route to different venues).
"""
from __future__ import annotations

import os
from typing import Optional

from database.client import get_recent_advisory_signals, log_event

GERMAN_MORNING_ENABLED = (
    os.getenv("GERMAN_MORNING_WATCH_ENABLED", "true").strip().lower() != "false"
)
MAX_NAMES = int(os.getenv("GERMAN_MORNING_MAX_NAMES", "8"))
STRONG_GRADES = {"A+", "A", "B"}
_GRADE_RANK = {"A+": 4, "A": 3, "B": 2, "C": 1}


def _overnight_futures() -> dict:
    """ES/NQ move vs the prior settle → risk tone. Uses daily closes (the
    forming session's close reflects the current pre-open level); fast_info
    does not populate for futures, so download is the reliable path."""
    out = {"es_pct": None, "nq_pct": None, "tone": "unknown"}
    try:
        import yfinance as yf
        import pandas as pd
        for key, sym in (("es_pct", "ES=F"), ("nq_pct", "NQ=F")):
            try:
                df = yf.download(sym, period="5d", interval="1d", progress=False)
                if df is None or df.empty:
                    continue
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                closes = df["Close"].dropna()
                if len(closes) >= 2 and float(closes.iloc[-2]) > 0:
                    out[key] = round((float(closes.iloc[-1]) - float(closes.iloc[-2]))
                                     / float(closes.iloc[-2]) * 100, 2)
            except Exception:
                continue
        vals = [v for v in (out["es_pct"], out["nq_pct"]) if v is not None]
        if vals:
            avg = sum(vals) / len(vals)
            out["tone"] = "risk-on" if avg > 0.25 else "risk-off" if avg < -0.25 else "mixed"
    except Exception:
        pass
    return out


def _latest_session_strong_names() -> tuple:
    """Strong BUY advisory names from the most recent US session that produced
    signals (handles Mondays / holidays by taking the latest date present).
    Returns (picks, session_date)."""
    rows = get_recent_advisory_signals(days=4, market="US", limit=400)
    rows = [r for r in (rows or [])
            if str(r.get("side", "")).upper() == "BUY" and r.get("grade") in STRONG_GRADES]
    if not rows:
        return [], None
    day = lambda r: str(r.get("created_at") or "")[:10]
    latest = max((day(r) for r in rows), default=None)
    rows = [r for r in rows if day(r) == latest]
    best: dict = {}
    for r in rows:
        sym = r.get("data_symbol")
        if not sym:
            continue
        score = (_GRADE_RANK.get(r.get("grade"), 0), float(r.get("composite_score") or 0))
        if sym not in best or score > best[sym][0]:
            best[sym] = (score, r)
    picks = sorted((v[1] for v in best.values()),
                   key=lambda r: (_GRADE_RANK.get(r.get("grade"), 0),
                                  float(r.get("composite_score") or 0)),
                   reverse=True)
    return picks[:MAX_NAMES], latest


def _fmt_card(futures: dict, picks: list, session_date: Optional[str]) -> str:
    fpct = lambda p: f"{p:+.2f}%" if p is not None else "n/a"
    tone = futures.get("tone", "unknown")
    tone_emoji = {"risk-on": "🟢", "risk-off": "🔴", "mixed": "🟡"}.get(tone, "⚪")
    lines = [
        "🌅 **GERMAN BROKER MORNING WATCH** (TR / Scalable)",
        f"{tone_emoji} Overnight futures: ES {fpct(futures.get('es_pct'))} · "
        f"NQ {fpct(futures.get('nq_pct'))} → **{tone}**",
        "Venue: early market-maker pricing · **limit only** · thin liquidity",
        "",
    ]
    if picks:
        lines.append(f"Watch (from {session_date or 'prior'} US session):")
        for r in picks:
            g = r.get("grade")
            gm = {"A+": "🟢", "A": "🟢", "B": "🟡"}.get(g, "⚪")
            cur = str(r.get("currency") or "USD")
            emn, emx = r.get("entry_min"), r.get("entry_max")
            band = f" · {cur} {float(emn):.2f}–{float(emx):.2f}" if emn and emx else ""
            reason = "breakout" if float(r.get("breakout_quality") or 0) >= 0.5 else "momentum"
            lines.append(f"{gm} **{r.get('data_symbol')}** ({g}) · {reason}{band}")
    else:
        lines.append("No strong prior-session names — trade the tape with caution.")
    lines += [
        "",
        "⚠️ Futures-implied, *not* live US confirmation. Spreads widen before "
        "15:30 CEST — use limit orders and check the German quote before entering.",
    ]
    return "\n".join(lines)


def run_german_morning_watch() -> dict:
    """Build and post the pre-open German-broker watch card to Discord."""
    if not GERMAN_MORNING_ENABLED:
        log_event("INFO", "german_morning_watch_skipped", {"reason": "disabled"})
        return {"ran": False, "reason": "disabled"}
    futures = _overnight_futures()
    picks, session_date = _latest_session_strong_names()
    card = _fmt_card(futures, picks, session_date)
    try:
        from backend.agent import _send_discord_alert  # lazy — avoids circular import
        sent = _send_discord_alert(card)
    except Exception as e:
        log_event("WARN", "german_morning_watch_send_failed", {"error": str(e)[:160]})
        sent = False
    log_event("INFO", "german_morning_watch", {
        "sent": sent, "names": len(picks),
        "tone": futures.get("tone"), "session_date": session_date,
    })
    return {"ran": True, "sent": sent, "names": len(picks), "futures": futures}


if __name__ == "__main__":
    print(run_german_morning_watch())
