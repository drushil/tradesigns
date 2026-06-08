"""frontend/pages/advisory_learning.py — Advisory learning outcomes dashboard.

Displays:
  1. Advisory policy recommendations from the nightly learner (proposed → accepted/rejected)
  2. Pick-level scoreboard (signal correctness by grade / session window / regime)
  3. Execution-level scoreboard (entry policy quality by closure reason / grade)
"""
from __future__ import annotations

import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
from datetime import datetime, timezone, timedelta

from frontend.ui_help import column_config, help_text, metric, section_header
from frontend.ui_theme import apply_plotly_theme, metric_card, page_header, status_pill


# ── Tone helpers ──────────────────────────────────────────────────────────────
_STATUS_TONE = {
    "proposed": "info",
    "accepted": "positive",
    "rejected": "negative",
    "expired":  "neutral",
}
_BUCKET_TONE = {
    "tp1_hit":            "positive",
    "tp2_hit":            "positive",
    "stop_hit":           "negative",
    "expired_positive":   "neutral",
    "expired_negative":   "neutral",
    "pending":            "info",
}
_REC_TYPE_TONE = {
    "threshold": "info",
    "gate":      "warning",
    "filter":    "neutral",
    "weight":    "positive",
}


def _pct(v, decimals=1) -> str:
    if v is None:
        return "—"
    return f"{float(v)*100:.{decimals}f}%"


def _fmt_score(v) -> str:
    if v is None:
        return "—"
    return f"{float(v):.3f}"


def _fmt_r(v) -> str:
    if v is None:
        return "—"
    return f"{float(v):+.2f}R"


# ── Section 1: policy recommendations ────────────────────────────────────────

def _render_recommendations(client):
    section_header("Policy Recommendations", help_key=None)

    try:
        from database.client import get_proposed_advisory_policy_recommendations
        recs = get_proposed_advisory_policy_recommendations(limit=100)
    except Exception as e:
        st.warning(f"Could not load recommendations: {e}")
        return

    # Also load all statuses for the history panel
    try:
        all_recs_resp = (
            client.table("advisory_policy_recommendations")
            .select("*")
            .order("computed_at", desc=True)
            .limit(200)
            .execute()
        )
        all_recs = all_recs_resp.data or []
    except Exception:
        all_recs = recs

    proposed = [r for r in all_recs if r.get("status") == "proposed"]
    accepted = [r for r in all_recs if r.get("status") == "accepted"]
    rejected = [r for r in all_recs if r.get("status") == "rejected"]
    expired  = [r for r in all_recs if r.get("status") == "expired"]

    c1, c2, c3, c4 = st.columns(4)
    metric_card(c1, "Proposed",  len(proposed), tone="info")
    metric_card(c2, "Accepted",  len(accepted), tone="positive")
    metric_card(c3, "Rejected",  len(rejected), tone="negative")
    metric_card(c4, "Expired",   len(expired),  tone="neutral")

    if not proposed:
        st.info("No proposed recommendations — learner hasn't run yet or all are actioned.")
        _render_recommendation_history(all_recs)
        return

    df = pd.DataFrame(proposed)
    for col in ["scope", "scope_value", "field_name", "suggested_value",
                "current_value", "recommendation_type", "status", "evidence_json",
                "notes", "computed_at"]:
        if col not in df.columns:
            df[col] = None

    # Format display columns
    df["rec_type_pill"] = df["recommendation_type"].apply(
        lambda t: status_pill(str(t or "—"), _REC_TYPE_TONE.get(str(t), "neutral"))
    )
    df["status_pill"] = df["status"].apply(
        lambda s: status_pill(str(s or "—"), _STATUS_TONE.get(str(s), "neutral"))
    )
    df["age"] = df["computed_at"].apply(
        lambda d: f"{(datetime.now(timezone.utc) - datetime.fromisoformat(d.replace('Z','+00:00'))).days}d ago"
        if d else "—"
    )

    # Display as an HTML table for pill rendering
    display_cols = ["scope", "scope_value", "field_name", "current_value",
                    "suggested_value", "rec_type_pill", "status_pill", "age"]
    html_rows = ""
    for _, row in df[display_cols].iterrows():
        cells = "".join(f"<td style='padding:6px 10px;border-bottom:1px solid #2d2d2d'>{v}</td>"
                        for v in row)
        html_rows += f"<tr>{cells}</tr>"

    headers = ["Scope", "Value", "Field", "Current", "Recommended", "Type", "Status", "Age"]
    header_html = "".join(
        f"<th style='padding:6px 10px;text-align:left;color:#888;font-weight:500;font-size:12px'>{h}</th>"
        for h in headers
    )
    table_html = f"""
    <div style='overflow-x:auto'>
    <table style='width:100%;border-collapse:collapse;font-size:13px'>
      <thead><tr style='border-bottom:2px solid #333'>{header_html}</tr></thead>
      <tbody>{html_rows}</tbody>
    </table></div>"""
    st.markdown(table_html, unsafe_allow_html=True)

    # Rationale expanders
    with st.expander("Evidence details"):
        for _, row in df.iterrows():
            label = f"{row.get('scope_value','?')} · {row.get('field_name','?')}"
            evidence = row.get("evidence_json") or row.get("notes") or "no detail"
            st.markdown(f"**{label}** — `{evidence}`")

    _render_recommendation_history(all_recs)


def _render_recommendation_history(all_recs: list):
    non_proposed = [r for r in all_recs if r.get("status") != "proposed"]
    if not non_proposed:
        return
    with st.expander("History (accepted / rejected / expired)"):
        df = pd.DataFrame(non_proposed)
        st.dataframe(
            df[["scope", "scope_value", "field_name", "suggested_value",
                "recommendation_type", "status", "computed_at", "status_changed_at"]].head(50),
            use_container_width=True,
        )


# ── Section 2: pick scoreboard ────────────────────────────────────────────────

def _render_pick_scoreboard(client, lookback_days: int):
    section_header("Pick-Level Scoreboard", help_key=None)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()

    try:
        resp = (
            client.rpc("advisory_pick_scoreboard", {})
            .execute()
        )
        rows = resp.data or []
    except Exception:
        # Fall back to raw query if view isn't callable as RPC
        try:
            resp = (
                client.table("advisory_pick_scoreboard")
                .select("*")
                .execute()
            )
            rows = resp.data or []
        except Exception as e:
            st.warning(f"Could not load pick scoreboard: {e}")
            return

    if not rows:
        # Build from advisory_signals directly
        try:
            resp2 = (
                client.table("advisory_signals")
                .select("grade,session_window,regime_at_pick,direction_correct_60m,"
                        "direction_correct_5d,pick_outcome_bucket,forward_return_5d,composite_score")
                .gte("created_at", cutoff)
                .not_.is_("pick_outcome_bucket", "null")
                .execute()
            )
            rows = resp2.data or []
        except Exception as e:
            st.warning(f"Could not load advisory signals for pick scoreboard: {e}")
            return

    if not rows:
        st.info("No scored picks yet — replay runs after market close.")
        return

    df = pd.DataFrame(rows)

    # Normalise column names from the view vs raw query
    grade_col   = "grade"   if "grade"   in df.columns else None
    window_col  = "session_window" if "session_window" in df.columns else None
    correct_col = "direction_correct_60m" if "direction_correct_60m" in df.columns else None
    correct5_col = "direction_correct_5d" if "direction_correct_5d" in df.columns else None
    bucket_col  = "pick_outcome_bucket" if "pick_outcome_bucket" in df.columns else None
    fwd5_col    = "forward_return_5d" if "forward_return_5d" in df.columns else None

    # ── KPIs ──────────────────────────────────────────────────────────────────
    total = len(df)
    hit60 = df[correct_col].sum() / total if correct_col and total else None
    hit5d = df[correct5_col].sum() / total if correct5_col and total else None
    _has_fwd5d = fwd5_col and fwd5_col in df.columns and df[fwd5_col].notna().any()
    avg5d = df[fwd5_col].mean() if _has_fwd5d else None

    c1, c2, c3, c4 = st.columns(4)
    metric_card(c1, "Scored Picks",  total)
    metric_card(c2, "60m Hit Rate",  _pct(hit60), tone="positive" if hit60 and hit60 > 0.5 else "negative")
    metric_card(c3, "5d Hit Rate",   _pct(hit5d),  tone="positive" if hit5d and hit5d > 0.5 else "negative")
    metric_card(c4, "Avg 5d Fwd %",  f"{avg5d:.2f}%" if avg5d is not None else "—",
                tone="positive" if avg5d and avg5d > 0 else "negative")

    # ── By grade ──────────────────────────────────────────────────────────────
    if grade_col and grade_col in df.columns:
        grade_group = (
            df.groupby(grade_col)
            .agg(
                count=(correct_col or grade_col, "count"),
                hit_60m=(correct_col, "mean") if correct_col else (grade_col, "count"),
                hit_5d=(correct5_col, "mean") if correct5_col else (grade_col, "count"),
                avg_fwd5d=(fwd5_col, "mean") if fwd5_col else (grade_col, "count"),
            )
            .reset_index()
        )

        fig = go.Figure()
        grade_order = [g for g in ["A+", "A", "B", "C"] if g in grade_group[grade_col].values]
        grade_colors = {"A+": "#00d4a0", "A": "#4fc3f7", "B": "#ffb74d", "C": "#ff5c5c"}

        if correct_col in grade_group.columns:
            fig.add_trace(go.Bar(
                x=grade_group[grade_col],
                y=(grade_group["hit_60m"] * 100).round(1),
                name="60m Hit %",
                marker_color=[grade_colors.get(g, "#aaa") for g in grade_group[grade_col]],
                opacity=0.9,
            ))
        if correct5_col in grade_group.columns:
            fig.add_trace(go.Bar(
                x=grade_group[grade_col],
                y=(grade_group["hit_5d"] * 100).round(1),
                name="5d Hit %",
                marker_color=[grade_colors.get(g, "#aaa") for g in grade_group[grade_col]],
                opacity=0.5,
            ))

        fig.update_layout(barmode="group", xaxis_title="Grade", yaxis_title="Hit Rate %",
                          title="Direction Correctness by Grade")
        apply_plotly_theme(fig, height=300)
        st.plotly_chart(fig, use_container_width=True)

    # ── By session window ─────────────────────────────────────────────────────
    if window_col and window_col in df.columns and df[window_col].notna().any():
        win_group = (
            df.dropna(subset=[window_col])
            .groupby(window_col)
            .agg(
                count=(window_col, "count"),
                hit_60m=(correct_col, "mean") if correct_col else (window_col, "count"),
            )
            .reset_index()
            .sort_values("count", ascending=False)
        )
        fig2 = px.bar(
            win_group, x=window_col, y="count",
            color="hit_60m" if correct_col in win_group.columns else window_col,
            color_continuous_scale="RdYlGn",
            range_color=[0, 1],
            labels={"count": "Picks", window_col: "Session", "hit_60m": "60m Hit"},
            title="Pick Volume & Hit Rate by Session Window",
        )
        apply_plotly_theme(fig2, height=280)
        st.plotly_chart(fig2, use_container_width=True)

    # ── Outcome bucket breakdown ──────────────────────────────────────────────
    if bucket_col and bucket_col in df.columns:
        bkt = df[bucket_col].value_counts().reset_index()
        bkt.columns = ["bucket", "count"]
        bucket_colors = {
            "tp1_hit": "#00d4a0", "tp2_hit": "#4fc3f7",
            "stop_hit": "#ff5c5c", "expired_positive": "#ffb74d",
            "expired_negative": "#888", "pending": "#555",
        }
        fig3 = px.pie(
            bkt, names="bucket", values="count",
            color="bucket",
            color_discrete_map=bucket_colors,
            title="Pick Outcome Distribution",
        )
        apply_plotly_theme(fig3, height=280)
        st.plotly_chart(fig3, use_container_width=True)


# ── Section 3: execution scoreboard ──────────────────────────────────────────

def _render_execution_scoreboard(client, lookback_days: int):
    section_header("Execution-Level Scoreboard", help_key=None)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()

    try:
        resp = (
            client.table("advisory_execution_scoreboard")
            .select("*")
            .execute()
        )
        rows = resp.data or []
    except Exception:
        rows = []

    if not rows:
        # Fall back to raw sims query
        try:
            resp2 = (
                client.table("advisory_auto_simulations")
                .select("entry_policy,closure_reason,r_multiple,entry_policy_quality,"
                        "sim_version,status,market")
                .gte("created_at", cutoff)
                .not_.is_("r_multiple", "null")
                .execute()
            )
            rows = resp2.data or []
        except Exception as e:
            st.warning(f"Could not load execution scoreboard: {e}")
            return

    if not rows:
        st.info("No closed simulations with R-multiples yet.")
        return

    df = pd.DataFrame(rows)

    # ── KPIs ──────────────────────────────────────────────────────────────────
    r_col = "avg_r" if "avg_r" in df.columns else ("r_multiple" if "r_multiple" in df.columns else None)
    epq_col = "avg_epq" if "avg_epq" in df.columns else ("entry_policy_quality" if "entry_policy_quality" in df.columns else None)

    if r_col:
        avg_r = df[r_col].mean()
        pos_r = (df[r_col] > 0).sum() / len(df)
    else:
        avg_r = pos_r = None

    c1, c2, c3 = st.columns(3)
    metric_card(c1, "Closed Sims",   len(df))
    metric_card(c2, "Avg R-Multiple", _fmt_r(avg_r),
                tone="positive" if avg_r and avg_r > 0 else "negative")
    metric_card(c3, "Positive R %",   _pct(pos_r),
                tone="positive" if pos_r and pos_r > 0.5 else "neutral")

    # ── R by entry policy ─────────────────────────────────────────────────────
    policy_col = "entry_policy" if "entry_policy" in df.columns else None
    if policy_col and r_col:
        pol_group = (
            df.groupby(policy_col)[r_col]
            .agg(["count", "mean"])
            .reset_index()
            .rename(columns={"mean": "avg_r", "count": "n"})
            .sort_values("avg_r", ascending=False)
        )
        fig = go.Figure(go.Bar(
            x=pol_group[policy_col],
            y=pol_group["avg_r"].round(3),
            marker_color=["#00d4a0" if v > 0 else "#ff5c5c" for v in pol_group["avg_r"]],
            text=pol_group["n"].apply(lambda n: f"n={n}"),
            textposition="outside",
        ))
        fig.add_hline(y=0, line_dash="dot", line_color="#555")
        fig.update_layout(xaxis_title="Entry Policy", yaxis_title="Avg R-Multiple",
                          title="R-Multiple by Entry Policy")
        apply_plotly_theme(fig, height=300)
        st.plotly_chart(fig, use_container_width=True)

    # ── R by closure reason ───────────────────────────────────────────────────
    closure_col = "closure_reason" if "closure_reason" in df.columns else None
    if closure_col and r_col:
        clo_group = (
            df.dropna(subset=[closure_col])
            .groupby(closure_col)[r_col]
            .agg(["count", "mean"])
            .reset_index()
            .rename(columns={"mean": "avg_r", "count": "n"})
            .sort_values("avg_r", ascending=False)
        )
        fig2 = go.Figure(go.Bar(
            x=clo_group[closure_col],
            y=clo_group["avg_r"].round(3),
            marker_color=["#4fc3f7" if v > 0 else "#ff9800" for v in clo_group["avg_r"]],
            text=clo_group["n"].apply(lambda n: f"n={n}"),
            textposition="outside",
        ))
        fig2.add_hline(y=0, line_dash="dot", line_color="#555")
        fig2.update_layout(xaxis_title="Closure Reason", yaxis_title="Avg R-Multiple",
                           title="R-Multiple by Closure Reason")
        apply_plotly_theme(fig2, height=300)
        st.plotly_chart(fig2, use_container_width=True)

    # ── Entry policy quality distribution ────────────────────────────────────
    if epq_col and epq_col in df.columns and df[epq_col].notna().any():
        epq_data = df[epq_col].dropna()
        fig3 = go.Figure(go.Histogram(
            x=epq_data,
            nbinsx=20,
            marker_color="#4fc3f7",
            opacity=0.8,
        ))
        fig3.update_layout(xaxis_title="Entry Policy Quality (0=low, 1=high)",
                           yaxis_title="Count",
                           title="Entry Quality Distribution (0 = filled at band top)")
        apply_plotly_theme(fig3, height=260)
        st.plotly_chart(fig3, use_container_width=True)

    # ── Raw table ─────────────────────────────────────────────────────────────
    with st.expander("Raw execution data"):
        show_cols = [c for c in [policy_col, closure_col, r_col, epq_col, "sim_version", "status"]
                     if c and c in df.columns]
        st.dataframe(df[show_cols].head(200), use_container_width=True)


# ── Main render ───────────────────────────────────────────────────────────────

def render():
    try:
        from database.client import get_client
        client = get_client()
    except Exception as e:
        st.error(f"DB error: {e}")
        return

    with st.sidebar:
        st.markdown("### Advisory Learning")
        lookback_days = st.slider("Look-back (days)", 7, 90, 30,
                                  help=help_text("Look-back (days)"))
        show_recs    = st.checkbox("Policy Recommendations", value=True)
        show_picks   = st.checkbox("Pick Scoreboard",        value=True)
        show_exec    = st.checkbox("Execution Scoreboard",   value=True)

    page_header(
        "Advisory Learning",
        "Nightly-learner recommendations, pick-level outcomes, and execution quality.",
        eyebrow="Learning",
        pills=[
            status_pill(f"{lookback_days}d", "info"),
            status_pill("advisory-first", "neutral"),
        ],
    )

    if show_recs:
        _render_recommendations(client)
        st.divider()

    if show_picks:
        _render_pick_scoreboard(client, lookback_days)
        st.divider()

    if show_exec:
        _render_execution_scoreboard(client, lookback_days)
