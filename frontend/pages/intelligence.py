"""Advisory intelligence: outcomes, execution evidence, journal, and recommendations."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from frontend.ui_theme import metric_card, modern_section, page_header, status_pill


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _fmt_pct(value, digits: int = 2) -> str:
    return f"{_safe_float(value):+.{digits}f}%"


def _fmt_number(value, digits: int = 2) -> str:
    return f"{_safe_float(value):.{digits}f}"


def _fmt_time(value) -> str:
    if not value:
        return "-"
    try:
        parsed = pd.to_datetime(value, utc=True, errors="coerce")
        if pd.isna(parsed):
            return str(value)
        return parsed.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)


def _weighted_average(rows: list[dict], value_key: str, weight_key: str) -> float | None:
    total_weight = 0
    total_value = 0.0
    for row in rows:
        weight = _safe_int(row.get(weight_key))
        if weight <= 0 or row.get(value_key) is None:
            continue
        total_weight += weight
        total_value += _safe_float(row.get(value_key)) * weight
    if total_weight <= 0:
        return None
    return total_value / total_weight


def _to_dataframe(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(rows)
    existing = [col for col in columns if col in df.columns]
    return df[existing].copy() if existing else df


def _render_pick_scoreboard(rows: list[dict], fallback_rows: list[dict]):
    if rows:
        df = _to_dataframe(
            rows,
            [
                "utc_date", "market", "mode", "grade", "side", "picks_scored",
                "win_rate_60m", "avg_fwd_15m", "avg_fwd_30m", "avg_fwd_60m",
                "avg_max_favorable_pct", "avg_max_adverse_pct",
            ],
        )
        for col in ["win_rate_60m", "avg_fwd_15m", "avg_fwd_30m", "avg_fwd_60m"]:
            if col in df.columns:
                df[col] = df[col].map(lambda value: _fmt_pct(value))
        st.dataframe(df, hide_index=True, width="stretch")
        return

    if fallback_rows:
        st.caption("Showing raw scored alerts because the advisory pick scoreboard view is not available yet.")
        df = _to_dataframe(
            fallback_rows,
            [
                "created_at", "data_symbol", "market", "mode", "grade", "side",
                "composite_score", "breakout_quality", "forward_return_15m",
                "forward_return_30m", "forward_return_60m", "max_favorable_pct", "max_adverse_pct",
            ],
        )
        st.dataframe(df, hide_index=True, width="stretch")
        return

    st.info("No scored advisory picks yet.")


def _render_execution_scoreboard(rows: list[dict]):
    if not rows:
        st.info("No simulator or execution outcome rows yet.")
        return
    df = _to_dataframe(
        rows,
        [
            "utc_date", "market", "mode", "policy_name", "trades_closed", "win_rate",
            "avg_r", "median_r", "avg_pnl_pct", "stops", "targets", "expired",
        ],
    )
    for col in ["win_rate", "avg_pnl_pct"]:
        if col in df.columns:
            df[col] = df[col].map(lambda value: _fmt_pct(value))
    for col in ["avg_r", "median_r"]:
        if col in df.columns:
            df[col] = df[col].map(lambda value: _fmt_number(value))
    st.dataframe(df, hide_index=True, width="stretch")


def _render_journal(manual: list[dict], auto: list[dict]):
    rows = []
    for source, trade_rows in (("Manual", manual), ("Advisory Auto", auto)):
        for row in trade_rows:
            rows.append({
                "source": source,
                "created_at": row.get("created_at"),
                "exit_time": row.get("exit_time"),
                "ticker": row.get("ticker"),
                "side": row.get("side"),
                "status": row.get("status") or row.get("exit_reason") or "-",
                "pnl_eur": _safe_float(row.get("pnl_eur")),
                "net_pnl_pct": _safe_float(row.get("net_pnl_pct") or row.get("pnl_pct")),
                "advisory_signal_id": row.get("advisory_signal_id"),
            })

    if not rows:
        st.info("No advisory-linked journal entries yet.")
        return

    total_pnl = sum(row["pnl_eur"] for row in rows)
    closed = [row for row in rows if row.get("exit_time")]
    wins = [row for row in closed if row["pnl_eur"] > 0 or row["net_pnl_pct"] > 0]
    c1, c2, c3 = st.columns(3)
    metric_card(c1, "Journal rows", str(len(rows)))
    metric_card(c2, "Closed win rate", f"{(len(wins) / len(closed) * 100) if closed else 0:.1f}%")
    metric_card(c3, "Closed P&L", f"€{total_pnl:+.2f}", tone="positive" if total_pnl >= 0 else "negative")

    df = pd.DataFrame(rows)
    df["created_at"] = df["created_at"].map(_fmt_time)
    df["exit_time"] = df["exit_time"].map(_fmt_time)
    df["pnl_eur"] = df["pnl_eur"].map(lambda value: f"€{value:+.2f}")
    df["net_pnl_pct"] = df["net_pnl_pct"].map(lambda value: _fmt_pct(value))
    st.dataframe(df, hide_index=True, width="stretch")


def _render_recommendations(rows: list[dict]):
    if not rows:
        st.info("No proposed advisory policy recommendations right now.")
        return

    top = rows[:6]
    for row in top:
        field = str(row.get("field_name") or row.get("recommendation_type") or "policy")
        scope = f"{row.get('scope') or 'global'}:{row.get('scope_value') or '*'}"
        confidence = _safe_float(row.get("confidence"))
        tone = "positive" if confidence >= 0.75 else "warning" if confidence >= 0.55 else "info"
        with st.container(border=True):
            left, right = st.columns([2.4, 1])
            with left:
                st.markdown(f"**{field}**")
                st.caption(scope)
                st.write(
                    f"{row.get('current_value', '-')}"
                    f" → {row.get('suggested_value', '-')}"
                )
            with right:
                st.markdown(status_pill(f"confidence {confidence:.0%}", tone), unsafe_allow_html=True)
                st.caption(f"sample {row.get('sample_size') or '-'}")

    df = _to_dataframe(
        rows,
        [
            "computed_at", "scope", "scope_value", "recommendation_type", "field_name",
            "current_value", "suggested_value", "sample_size", "hit_rate", "confidence", "status",
        ],
    )
    if "computed_at" in df.columns:
        df["computed_at"] = df["computed_at"].map(_fmt_time)
    st.dataframe(df, hide_index=True, width="stretch")


def render():
    page_header(
        "Intelligence",
        "Advisory outcomes, execution evidence, trade journal, and proposed policy changes.",
        eyebrow="Evidence",
        pills=[status_pill("pick-level + execution", "info")],
    )

    try:
        from database import client as db_client
    except Exception as exc:
        st.error(f"Database client unavailable: {str(exc)[:180]}")
        return

    st.markdown(
        """
        <div class="td-toolbar">
          <div>
            <div class="td-toolbar-title">Evidence window</div>
            <div class="td-toolbar-copy">Use this page to separate signal quality, execution quality, and your own decisions.</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    c1, c2, _ = st.columns([1, 1, 2])
    with c1:
        days = st.segmented_control("Window", [30, 60, 90], default=90, width="stretch")
    with c2:
        market = st.segmented_control("Market", ["ALL", "US", "EU"], default="ALL", width="stretch")

    pick_rows = db_client.get_advisory_pick_scoreboard(days_back=int(days), market=market)
    execution_rows = db_client.get_advisory_execution_scoreboard(days_back=int(days), market=market)
    fallback_rows = db_client.get_advisory_scoreboard(days_back=int(days), market=market)
    recommendations = db_client.get_proposed_advisory_policy_recommendations(limit=50)
    manual_trades = db_client.get_advisory_trades(days=int(days))
    auto_trades = db_client.get_advisory_auto_trades(days=int(days))

    pick_count = sum(_safe_int(row.get("picks_scored")) for row in pick_rows) or len(fallback_rows)
    avg_60 = _weighted_average(pick_rows, "avg_fwd_60m", "picks_scored")
    if avg_60 is None and fallback_rows:
        avg_60 = sum(_safe_float(row.get("forward_return_60m")) for row in fallback_rows) / max(len(fallback_rows), 1)
    closed_count = sum(_safe_int(row.get("trades_closed")) for row in execution_rows)
    avg_r = _weighted_average(execution_rows, "avg_r", "trades_closed")

    m1, m2, m3, m4 = st.columns(4)
    metric_card(m1, "Scored picks", str(pick_count), help_text="Pick-level advisory cards with forward outcome evidence.")
    metric_card(
        m2,
        "Avg 60m move",
        "-" if avg_60 is None else _fmt_pct(avg_60),
        tone="positive" if (avg_60 or 0) >= 0 else "negative",
    )
    metric_card(m3, "Closed sim trades", str(closed_count))
    metric_card(m4, "Recommendations", str(len(recommendations)))

    st.markdown(
        """
        <div class="td-insight-grid">
          <div class="td-insight-card">
            <strong>Outcomes</strong>
            <div>Answers whether advisory cards are directionally useful before any execution assumptions.</div>
          </div>
          <div class="td-insight-card">
            <strong>Journal</strong>
            <div>Separates what the engine suggested from what you actually chose to do.</div>
          </div>
          <div class="td-insight-card">
            <strong>Recommendations</strong>
            <div>Turns outcome evidence into proposed threshold and policy changes.</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    outcomes_tab, execution_tab, journal_tab, recs_tab = st.tabs(
        ["Outcomes", "Execution", "Journal", "Recommendations"]
    )

    with outcomes_tab:
        modern_section("Pick Outcomes", "Whether the advisory engine is pointing in the right direction.")
        _render_pick_scoreboard(pick_rows, fallback_rows)

    with execution_tab:
        modern_section("Execution Outcomes", "Whether the simulated or dry-run policy is using picks well.")
        if avg_r is not None:
            st.caption(f"Weighted average R over the selected window: {_fmt_number(avg_r)}")
        _render_execution_scoreboard(execution_rows)

    with journal_tab:
        modern_section("Trade Journal", "What you actually did with advisory ideas.")
        _render_journal(manual_trades, auto_trades)

    with recs_tab:
        modern_section("Policy Recommendations", "Proposed changes generated from advisory outcome evidence.")
        _render_recommendations(recommendations)
