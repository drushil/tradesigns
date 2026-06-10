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


def _display_table(df: pd.DataFrame):
    if df.empty:
        st.dataframe(df, hide_index=True, width="stretch")
        return
    df = df.rename(columns={
        "utc_date": "Date",
        "created_at": "Created",
        "simulated_at": "Simulated",
        "closed_at": "Closed",
        "fill_at": "Filled",
        "data_symbol": "Stock",
        "ticker": "Stock",
        "market": "Market",
        "mode": "Mode",
        "entry_policy": "Entry policy",
        "policy_name": "Policy",
        "grade": "Grade",
        "side": "Side",
        "session_window": "Session",
        "regime": "Regime",
        "picks_total": "Picks",
        "picks_scored": "Scored",
        "picks_tp1_hit": "TP1",
        "picks_stop_hit": "Stops",
        "picks_dir_correct_60m": "Dir 60m",
        "picks_dir_correct_5d": "Dir 5d",
        "avg_fwd_15m": "Avg 15m",
        "avg_fwd_30m": "Avg 30m",
        "avg_fwd_60m": "Avg 60m",
        "avg_fwd_5d": "Avg 5d",
        "avg_mfe_pct": "Avg MFE",
        "avg_mae_pct": "Avg MAE",
        "avg_max_favorable_pct": "Avg MFE",
        "avg_max_adverse_pct": "Avg MAE",
        "composite_score": "Composite",
        "breakout_quality": "Breakout",
        "forward_return_15m": "Fwd 15m",
        "forward_return_30m": "Fwd 30m",
        "forward_return_60m": "Fwd 60m",
        "forward_return_5d": "Fwd 5d",
        "max_favorable_pct": "MFE",
        "max_adverse_pct": "MAE",
        "pick_outcome_bucket": "Outcome",
        "status": "Status",
        "closure_reason": "Close reason",
        "total": "Total",
        "filled_or_closed": "Filled/closed",
        "tp1_hit": "TP1",
        "tp2_hit": "TP2",
        "stopped": "Stops",
        "eod_closed": "EOD",
        "expired": "Expired",
        "cancelled_weak": "Cancelled weak",
        "avg_r": "Avg R",
        "r_multiple": "R",
        "mfe_pct": "MFE",
        "mae_pct": "MAE",
        "fill_price": "Fill",
        "last_price": "Last",
        "entry_min": "Entry min",
        "entry_max": "Entry max",
        "stop_price": "Stop",
        "target_1": "T1",
        "target_2": "T2",
        "entry_policy_quality": "Entry quality",
        "trade_source": "Source",
        "pnl_eur": "P&L EUR",
        "net_pnl_pct": "P&L %",
        "advisory_signal_id": "Signal ID",
    })
    st.dataframe(df, hide_index=True, width="stretch")


def _render_pick_scoreboard(rows: list[dict], fallback_rows: list[dict]):
    if rows:
        df = _to_dataframe(
            rows,
            [
                "utc_date", "market", "session_window", "regime", "grade", "side",
                "picks_total", "picks_scored", "picks_tp1_hit", "picks_stop_hit",
                "picks_dir_correct_60m", "picks_dir_correct_5d",
                "avg_fwd_60m", "avg_fwd_5d", "avg_mfe_pct", "avg_mae_pct",
            ],
        )
        for col in ["avg_fwd_60m", "avg_fwd_5d", "avg_mfe_pct", "avg_mae_pct"]:
            if col in df.columns:
                df[col] = df[col].map(lambda value: _fmt_pct(value))
        _display_table(df)
        return

    if fallback_rows:
        st.caption("Showing raw scored alerts because the advisory pick scoreboard view is not available yet.")
        df = _to_dataframe(
            fallback_rows,
            [
                "created_at", "data_symbol", "market", "mode", "grade", "side",
                "composite_score", "breakout_quality", "forward_return_15m",
                "forward_return_30m", "forward_return_60m", "forward_return_5d",
                "max_favorable_pct", "max_adverse_pct", "pick_outcome_bucket",
            ],
        )
        _display_table(df)
        return

    st.info("No scored advisory picks yet.")


def _render_pick_details(rows: list[dict]):
    if not rows:
        st.info("No stock-level scored advisory picks yet.")
        return
    df = _to_dataframe(
        rows,
        [
            "created_at", "data_symbol", "market", "mode", "grade", "side",
            "composite_score", "breakout_quality", "forward_return_15m",
            "forward_return_30m", "forward_return_60m", "forward_return_5d",
            "max_favorable_pct", "max_adverse_pct", "pick_outcome_bucket",
        ],
    )
    for col in [
        "forward_return_15m", "forward_return_30m", "forward_return_60m",
        "forward_return_5d", "max_favorable_pct", "max_adverse_pct",
    ]:
        if col in df.columns:
            df[col] = df[col].map(lambda value: _fmt_pct(value))
    if "created_at" in df.columns:
        df["created_at"] = df["created_at"].map(_fmt_time)
    _display_table(df)


def _render_execution_scoreboard(rows: list[dict]):
    if not rows:
        st.info("No simulator or execution outcome rows yet.")
        return
    df = _to_dataframe(
        rows,
        [
            "utc_date", "market", "entry_policy", "grade", "side", "source",
            "total", "filled_or_closed", "tp1_hit", "tp2_hit", "stopped",
            "eod_closed", "expired", "cancelled_weak", "avg_r",
            "avg_mfe_pct", "avg_mae_pct", "avg_entry_quality",
        ],
    )
    for col in ["avg_mfe_pct", "avg_mae_pct"]:
        if col in df.columns:
            df[col] = df[col].map(lambda value: _fmt_pct(value))
    for col in ["avg_r", "avg_entry_quality"]:
        if col in df.columns:
            df[col] = df[col].map(lambda value: _fmt_number(value))
    _display_table(df)


def _render_simulation_details(rows: list[dict]):
    if not rows:
        st.info("No stock-level simulator rows yet.")
        return
    df = _to_dataframe(
        rows,
        [
            "simulated_at", "data_symbol", "market", "side", "grade", "alert_stage",
            "entry_policy", "status", "closure_reason", "fill_at", "fill_price",
            "closed_at", "last_price", "entry_min", "entry_max", "stop_price",
            "target_1", "target_2", "composite_score", "breakout_quality",
            "r_multiple", "mfe_pct", "mae_pct", "entry_policy_quality", "advisory_signal_id",
        ],
    )
    for col in ["simulated_at", "fill_at", "closed_at"]:
        if col in df.columns:
            df[col] = df[col].map(_fmt_time)
    for col in ["mfe_pct", "mae_pct"]:
        if col in df.columns:
            df[col] = df[col].map(lambda value: _fmt_pct(value))
    for col in ["r_multiple", "entry_policy_quality"]:
        if col in df.columns:
            df[col] = df[col].map(lambda value: _fmt_number(value))
    _display_table(df)


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
    simulation_details = db_client.get_advisory_auto_simulation_details(days_back=int(days), market=market)
    recommendations = db_client.get_proposed_advisory_policy_recommendations(limit=50)
    manual_trades = db_client.get_advisory_trades(days=int(days))
    auto_trades = db_client.get_advisory_auto_trades(days=int(days))

    pick_count = sum(_safe_int(row.get("picks_scored")) for row in pick_rows) or len(fallback_rows)
    avg_60 = _weighted_average(pick_rows, "avg_fwd_60m", "picks_scored")
    if avg_60 is None and fallback_rows:
        avg_60 = sum(_safe_float(row.get("forward_return_60m")) for row in fallback_rows) / max(len(fallback_rows), 1)
    closed_count = sum(_safe_int(row.get("filled_or_closed")) for row in execution_rows)
    avg_r = _weighted_average(execution_rows, "avg_r", "filled_or_closed")

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

    modern_section("Pick Outcome Aggregates", "Whether the advisory engine is pointing in the right direction.")
    _render_pick_scoreboard(pick_rows, fallback_rows)

    modern_section("Stock-Level Pick Details", "Individual advisory cards with stock, grade, score, and realized forward moves.")
    _render_pick_details(fallback_rows)

    st.divider()

    modern_section("Execution Aggregates", "Whether the simulated or dry-run policy is using picks well.")
    if avg_r is not None:
        st.caption(f"Weighted average R over the selected window: {_fmt_number(avg_r)}")
    _render_execution_scoreboard(execution_rows)

    modern_section("Stock-Level Simulation Details", "Individual simulated entries with stock, fill, stop, target, R, MFE, and MAE.")
    _render_simulation_details(simulation_details)

    st.divider()

    modern_section("Trade Journal", "What you actually did with advisory ideas.")
    _render_journal(manual_trades, auto_trades)

    st.divider()

    modern_section("Policy Recommendations", "Proposed changes generated from advisory outcome evidence.")
    _render_recommendations(recommendations)
