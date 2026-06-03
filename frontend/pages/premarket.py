"""frontend/pages/premarket.py - Pre-market radar snapshots."""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import timezone

import pandas as pd
import streamlit as st

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

from frontend.ui_theme import metric_card, status_pill


CLASS_TONE = {
    "gap_continuation_watch": "positive",
    "opening_range_watch": "info",
    "catalyst_watch": "info",
    "gap_fade_or_ignore": "warning",
    "ignore_wide_spread": "negative",
}


def _local_tz():
    if ZoneInfo is None:
        return timezone.utc
    return ZoneInfo(os.getenv("DASHBOARD_TIMEZONE", "Europe/Berlin"))


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _fmt_time(value) -> str:
    try:
        parsed = pd.to_datetime(value, utc=True, errors="coerce")
        if pd.isna(parsed):
            return "-"
        return parsed.tz_convert(_local_tz()).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value or "-")


def _safe_float(value, default=0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def render():
    st.title("Pre-Market Radar")
    st.caption("Read-only gap watchlist with catalyst, liquidity, and opening-plan context.")

    try:
        from database import client as db_client

        reader = getattr(db_client, "get_latest_premarket_radar_snapshots", None)
        if reader:
            rows = reader(limit=200)
        else:
            result = (db_client.get_client()
                      .table("premarket_radar_snapshots")
                      .select("*")
                      .order("cycle_started_at", desc=True)
                      .limit(200)
                      .execute())
            rows = result.data or []
    except Exception as e:
        message = str(e)[:180]
        if "premarket_radar_snapshots" in message and "schema cache" in message:
            st.info("Pre-market radar table is not available yet. Apply the Supabase migration to enable snapshots.")
        else:
            st.error(f"Could not load pre-market radar snapshots: {message}")
        return

    if not rows:
        st.info("No pre-market radar snapshots yet.")
        return

    latest_cycle = max(str(r.get("cycle_started_at") or "") for r in rows)
    latest = [r for r in rows if str(r.get("cycle_started_at") or "") == latest_cycle]
    latest.sort(key=lambda r: (_safe_float(r.get("radar_score")), abs(_safe_float(r.get("gap_pct")))), reverse=True)

    counts = Counter(str(r.get("classification") or "unknown") for r in latest)
    c1, c2, c3, c4 = st.columns(4)
    metric_card(c1, "Last cycle", _fmt_time(latest_cycle))
    metric_card(c2, "Candidates", str(len(latest)))
    metric_card(c3, "Top score", f"{max(_safe_float(r.get('radar_score')) for r in latest):.1f}")
    metric_card(c4, "Window", str(latest[0].get("session_window") or "-"))

    st.subheader("Latest Watchlist")
    for row in latest[:20]:
        classification = str(row.get("classification") or "unknown")
        with st.container(border=True):
            left, mid, right = st.columns([1.1, 1.2, 1.2])
            with left:
                st.markdown(f"### {row.get('ticker', '-')}")
                st.markdown(
                    status_pill(classification.replace("_", " "), CLASS_TONE.get(classification, "neutral")),
                    unsafe_allow_html=True,
                )
            with mid:
                st.metric("Gap", f"{_safe_float(row.get('gap_pct')):+.2f}%")
                st.caption(
                    f"PMH { _safe_float(row.get('premarket_high')):.2f} | "
                    f"PML { _safe_float(row.get('premarket_low')):.2f} | "
                    f"VWAP { _safe_float(row.get('premarket_vwap')):.2f}"
                )
            with right:
                rvol = row.get("premarket_rvol")
                rvol_text = "-" if rvol is None else f"{_safe_float(rvol):.2f}x"
                spread = row.get("spread_pct")
                spread_text = "-" if spread is None else f"{_safe_float(spread):.2f}%"
                st.metric("Score", f"{_safe_float(row.get('radar_score')):.1f}")
                st.caption(f"RVOL {rvol_text} | Spread {spread_text}")

            plan = str(row.get("opening_plan") or "")
            if plan:
                st.write(plan)
            reasons = _as_list(row.get("reasons_json"))
            if reasons:
                st.caption("Reasons: " + ", ".join(str(r) for r in reasons[:5]))
            headline = str(row.get("latest_headline") or "")
            if headline:
                st.caption("Catalyst: " + headline)

    if counts:
        st.subheader("Classification Mix")
        summary = pd.DataFrame(
            [{"classification": k.replace("_", " "), "count": v} for k, v in counts.items()]
        )
        st.dataframe(summary, hide_index=True, use_container_width=True)
