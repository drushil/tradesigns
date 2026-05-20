"""frontend/pages/eod_review.py — Daily post-market review dashboard."""
from __future__ import annotations

import json

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from frontend.ui_help import metric, section_title, column_config
from frontend.ui_theme import page_header, status_pill


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


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


def _safe_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _flatten_rows(rows: list[dict], keep: list[str]) -> pd.DataFrame:
    prepared = []
    for row in rows or []:
        item = {}
        for key in keep:
            value = row.get(key)
            if isinstance(value, dict):
                value = json.dumps(value, sort_keys=True)[:240]
            item[key] = value
        prepared.append(item)
    return pd.DataFrame(prepared)


def _render_table(rows: list[dict], columns: list[str], empty: str):
    if not rows:
        st.info(empty)
        return
    df = _flatten_rows(rows, columns)
    st.dataframe(df, width="stretch", hide_index=True, column_config=column_config(df.columns))


def _review_recommendations(review: dict, row: dict) -> list[dict]:
    recs = _as_list(row.get("recommendations_json"))
    if recs:
        return recs
    return _as_list(review.get("recommendations"))


def render():
    try:
        from database.client import get_daily_reviews

        reviews = get_daily_reviews(limit=14)
    except Exception as exc:
        st.error(f"Could not load daily reviews: {exc}")
        return

    if not reviews:
        st.info("No daily reviews stored yet. The post-market EOD job will populate this page.")
        return

    latest = reviews[0]
    metrics_json = _as_dict(latest.get("metrics_json"))
    review_json = _as_dict(latest.get("review_json"))
    trade = _as_dict(metrics_json.get("trade_summary"))
    blocked = _as_dict(metrics_json.get("blocked_opportunities"))
    near_miss = _as_dict(metrics_json.get("near_miss_distribution"))
    gate_activity = _as_dict(metrics_json.get("gate_activity"))
    shadow = _as_list(metrics_json.get("shadow_universe"))
    recommendations = _review_recommendations(review_json, latest)

    page_header(
        "EOD Review",
        "Post-market evidence for trades, gate activity, missed winners, and recommended config changes.",
        eyebrow="Daily Review",
        pills=[
            status_pill(str(latest.get("review_date", "latest")), "info"),
            status_pill(f"{int(near_miss.get('runner_count') or 0)} missed runners", "warning"),
            status_pill(f"{len(recommendations)} recommendations", "positive" if recommendations else "neutral"),
        ],
    )

    c1, c2, c3, c4, c5 = st.columns(5)
    metric(c1, "Review Date", latest.get("review_date", "—"))
    metric(c2, "Total P&L", f"€{_safe_float(trade.get('total_pnl_eur')):+.2f}")
    metric(c3, "Trades", int(trade.get("total_trades") or 0))
    metric(c4, "Win Rate", f"{_safe_float(trade.get('win_rate_pct')):.1f}%")
    metric(c5, "Missed runners", int(near_miss.get("runner_count") or 0))

    section_title("Latest Summary")
    summary = review_json.get("summary") or "Daily review stored without a text summary."
    confidence = _safe_float(review_json.get("confidence"))
    st.markdown(
        f"""
        <div class="signal-card">
          <div class="signal-name">Confidence {confidence:.0%}</div>
          <div style="font-size:15px;color:#ddd;line-height:1.55">{summary}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col_a, col_b = st.columns(2)
    with col_a:
        section_title("Worked")
        worked = _as_list(review_json.get("worked_well"))
        if worked:
            for item in worked[:4]:
                st.markdown(f"- {item}")
        else:
            st.caption("No positives captured in this review.")
    with col_b:
        section_title("Watch")
        failed = _as_list(review_json.get("did_not_work"))
        if failed:
            for item in failed[:4]:
                st.markdown(f"- {item}")
        else:
            st.caption("No watch items captured in this review.")

    st.markdown("---")
    section_title("Gate Activity")
    event_counts = _as_dict(gate_activity.get("event_counts"))
    if event_counts:
        sorted_items = sorted(event_counts.items(), key=lambda item: item[1], reverse=True)
        fig = go.Figure(go.Bar(
            x=[item[1] for item in sorted_items],
            y=[item[0] for item in sorted_items],
            orientation="h",
            marker_color="#00d4a0",
        ))
        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=max(220, 30 * len(sorted_items)),
            margin=dict(l=0, r=0, t=10, b=0),
            xaxis=dict(gridcolor="#1a1a1a"),
            yaxis=dict(gridcolor="#1a1a1a", autorange="reversed"),
        )
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("No gate activity was captured for this review.")

    top_veto = _as_list(gate_activity.get("top_veto_tickers"))
    if top_veto:
        _render_table(top_veto, ["ticker", "count"], "No veto ticker data.")

    st.markdown("---")
    col_c, col_d = st.columns(2)
    with col_c:
        section_title("Missed Winners")
        _render_table(
            _as_list(blocked.get("missed_winners")),
            [
                "ticker", "stage", "reason", "runner_severity", "threshold_gap",
                "max_favorable_pct", "max_adverse_pct", "close_after_pct", "setup_grade",
            ],
            "No replayed blocked candidates met the missed-winner bar.",
        )
    with col_d:
        section_title("Bad Avoids")
        _render_table(
            _as_list(blocked.get("bad_avoids")),
            ["ticker", "stage", "reason", "max_favorable_pct", "max_adverse_pct", "close_after_pct", "setup_grade"],
            "No bad-avoid replay rows for this review.",
        )

    section_title("Near-Threshold Distribution")
    n1, n2, n3, n4 = st.columns(4)
    metric(n1, "Total", int(near_miss.get("total") or 0))
    metric(n2, "Checked", int(near_miss.get("checked") or 0))
    metric(n3, "Runner count", int(near_miss.get("runner_count") or 0))
    metric(n4, "Median fav %", f"{_safe_float(near_miss.get('median_max_favorable_pct')):+.2f}%")
    if near_miss.get("guardrail"):
        st.caption(near_miss["guardrail"])
    _render_table(
        _as_list(near_miss.get("top_runners")),
        ["ticker", "stage", "reason", "score", "threshold", "threshold_gap", "max_favorable_pct", "close_after_pct"],
        "No near-threshold runners yet.",
    )

    section_title("Direction Error Candidates")
    direction_errors = _as_list(metrics_json.get("direction_error_candidates"))
    _render_table(
        direction_errors,
        ["ticker", "action", "stage", "reason", "max_favorable_pct", "max_adverse_pct", "close_after_pct", "signal_snapshot"],
        "No direction-error candidates were flagged.",
    )

    st.markdown("---")
    col_e, col_f = st.columns(2)
    with col_e:
        section_title("Shadow Universe")
        _render_table(
            shadow,
            ["ticker", "theme", "mentions", "threshold", "review_candidate", "evidence_days", "theme_relative_pct", "reason"],
            "No shadow universe candidates captured.",
        )
    with col_f:
        section_title("Recommendations")
        _render_table(
            recommendations,
            ["category", "variable", "suggested_value", "confidence", "evidence_days", "status", "reason", "command_text"],
            "No config recommendations captured.",
        )

    st.markdown("---")
    section_title("Recent Review History")
    rows = []
    for item in reviews:
        item_metrics = _as_dict(item.get("metrics_json"))
        item_review = _as_dict(item.get("review_json"))
        item_trade = _as_dict(item_metrics.get("trade_summary"))
        item_blocked = _as_dict(item_metrics.get("blocked_opportunities"))
        item_near = _as_dict(item_metrics.get("near_miss_distribution"))
        rows.append({
            "review_date": item.get("review_date"),
            "pnl_eur": _safe_float(item_trade.get("total_pnl_eur")),
            "trades": int(item_trade.get("total_trades") or 0),
            "win_rate_pct": _safe_float(item_trade.get("win_rate_pct")),
            "missed_winners": len(_as_list(item_blocked.get("missed_winners"))),
            "near_runners": int(item_near.get("runner_count") or 0),
            "recommendations": len(_review_recommendations(item_review, item)),
            "summary": item_review.get("summary"),
        })
    history = pd.DataFrame(rows)
    st.dataframe(history, width="stretch", hide_index=True, column_config=column_config(history.columns))
