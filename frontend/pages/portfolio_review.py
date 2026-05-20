"""
frontend/pages/portfolio_review.py
Weekly advisory portfolio review — observation and recommendation only.
Shows hold/trim/add/exit recommendations, thesis validity, and exposure alerts.
No trade execution from this page.
"""
import json

import streamlit as st
import plotly.graph_objects as go
import plotly.express as px

from database.client import get_portfolio_reviews
from frontend.ui_help import metric, section_title, selectbox
from frontend.ui_theme import page_header, status_pill

# ── Colour map for recommendations ───────────────────────────────────────────
_REC_COLOR = {
    "hold":      "#4caf50",
    "add":       "#2196f3",
    "trim":      "#ff9800",
    "exit":      "#f44336",
    "rebalance": "#9c27b0",
}
_THESIS_COLOR = {
    "valid":     "#4caf50",
    "weakening": "#ff9800",
    "broken":    "#f44336",
}


def render():
    reviews = get_portfolio_reviews(limit=12)

    if not reviews:
        page_header(
            "Weekly Portfolio Review",
            "Advisory portfolio observations, recommendation mix, thesis checks, and exposure alerts.",
            eyebrow="Portfolio Advisory",
        )
        st.info("No portfolio reviews yet. The first review runs Sunday 17:00 UTC.")
        return

    page_header(
        "Weekly Portfolio Review",
        "Advisory observations only: no automatic portfolio trades are placed from this page.",
        eyebrow="Portfolio Advisory",
        pills=[status_pill(f"{len(reviews)} reviews", "neutral")],
    )

    # ── Review selector ───────────────────────────────────────────────────────
    dates = [r["reviewed_at"][:10] for r in reviews]
    selected_date = selectbox("Select review week", dates)
    review = next(r for r in reviews if r["reviewed_at"][:10] == selected_date)

    # Parse JSON columns (Supabase may return strings or dicts)
    def _j(v):
        return json.loads(v) if isinstance(v, str) else (v or {})

    summary  = _j(review.get("summary", {}))
    alerts   = _j(review.get("alerts", []))
    positions = _j(review.get("positions", []))
    exposure = _j(review.get("exposure", {}))

    # ── Top KPIs ──────────────────────────────────────────────────────────────
    c1, c2, c3, c4, c5 = st.columns(5)
    metric(c1, "Equity (€)", f"{review.get('equity_eur', 0):,.2f}")
    metric(c2, "Positions", review.get("position_count", 0))
    metric(c3, "Hold", summary.get("hold", 0))
    metric(c4, "Trim / Exit", summary.get("trim", 0) + summary.get("exit", 0))
    metric(c5, "Add", summary.get("add", 0))

    # ── Alerts ───────────────────────────────────────────────────────────────
    if alerts:
        with st.expander(f"⚠️ {len(alerts)} concentration alert(s)", expanded=True):
            for a in alerts:
                st.warning(a)
    else:
        st.success("No concentration alerts.")

    st.divider()

    # ── Recommendation summary donut ──────────────────────────────────────────
    col_chart, col_table = st.columns([1, 2])

    with col_chart:
        section_title("Recommendation Mix", level=3)
        labels = list(summary.keys())
        values = list(summary.values())
        colors = [_REC_COLOR.get(l, "#888") for l in labels]
        fig = go.Figure(go.Pie(
            labels=labels, values=values,
            marker_colors=colors,
            hole=0.5,
            textinfo="label+value",
        ))
        fig.update_layout(
            template="plotly_dark",
            showlegend=False,
            margin=dict(t=10, b=10, l=10, r=10),
            height=260,
        )
        st.plotly_chart(fig, use_container_width=True)

    # ── Position detail table ─────────────────────────────────────────────────
    with col_table:
        section_title("Position Recommendations", level=3)
        if not positions:
            st.info("No open positions were reviewed.")
        else:
            for pos in sorted(positions, key=lambda p: (
                {"exit": 0, "trim": 1, "add": 2, "hold": 3}.get(p["recommendation"], 9)
            )):
                rec   = pos["recommendation"]
                color = _REC_COLOR.get(rec, "#888")
                t_col = _THESIS_COLOR.get(pos.get("thesis_status", "valid"), "#888")
                pnl   = pos.get("pnl_pct", 0)
                pnl_str = f"{'+' if pnl >= 0 else ''}{pnl:.2f}%"

                with st.container():
                    r1, r2, r3, r4 = st.columns([1.2, 1.2, 1, 2])
                    r1.markdown(f"**{pos['ticker']}**")
                    r2.markdown(
                        f"<span style='color:{color};font-weight:bold'>"
                        f"{rec.upper()}</span>",
                        unsafe_allow_html=True,
                    )
                    r3.markdown(
                        f"<span style='color:{t_col}'>{pos.get('thesis_status','?')}</span>",
                        unsafe_allow_html=True,
                    )
                    r4.markdown(pnl_str)

                rationale = pos.get("rationale", {})
                note = rationale.get("note", "")
                if note:
                    st.caption(f"↳ {note}")

    # ── Sector exposure bar ───────────────────────────────────────────────────
    st.divider()
    section_title("Sector Exposure", level=3)
    sector_pct = exposure.get("sector_pct", {})
    if sector_pct:
        sec_fig = px.bar(
            x=list(sector_pct.keys()),
            y=list(sector_pct.values()),
            labels={"x": "Sector", "y": "% of Equity"},
            template="plotly_dark",
            color=list(sector_pct.values()),
            color_continuous_scale="RdYlGn_r",
        )
        sec_fig.add_hline(y=35, line_dash="dot", line_color="red",
                          annotation_text="35% limit")
        sec_fig.update_layout(
            showlegend=False,
            coloraxis_showscale=False,
            height=280,
            margin=dict(t=20, b=20),
        )
        st.plotly_chart(sec_fig, use_container_width=True)
    else:
        st.info("No sector exposure data.")

    # ── Cash / deployed metrics ───────────────────────────────────────────────
    st.divider()
    m1, m2, m3 = st.columns(3)
    metric(m1, "Cash (€)",     f"{exposure.get('cash_eur', 0):,.2f}",
           f"{exposure.get('cash_pct', 0):.1f}% of equity")
    metric(m2, "Deployed (€)", f"{exposure.get('total_deployed', 0):,.2f}",
           f"{exposure.get('deployed_pct', 0):.1f}% of equity")
    metric(m3, "Positions",    exposure.get("position_count", 0))

    # ── Historical review trend ───────────────────────────────────────────────
    if len(reviews) > 1:
        st.divider()
        section_title("Review History", level=3)
        hist_dates = [r["reviewed_at"][:10] for r in reversed(reviews)]
        hist_exit  = [_j(r.get("summary", {})).get("exit", 0) for r in reversed(reviews)]
        hist_trim  = [_j(r.get("summary", {})).get("trim", 0) for r in reversed(reviews)]
        hist_hold  = [_j(r.get("summary", {})).get("hold", 0) for r in reversed(reviews)]
        hist_add   = [_j(r.get("summary", {})).get("add", 0) for r in reversed(reviews)]

        h_fig = go.Figure()
        for label, values, color in [
            ("Hold",  hist_hold, "#4caf50"),
            ("Add",   hist_add,  "#2196f3"),
            ("Trim",  hist_trim, "#ff9800"),
            ("Exit",  hist_exit, "#f44336"),
        ]:
            h_fig.add_trace(go.Bar(
                name=label, x=hist_dates, y=values,
                marker_color=color,
            ))
        h_fig.update_layout(
            barmode="stack",
            template="plotly_dark",
            height=280,
            margin=dict(t=20, b=20),
            legend=dict(orientation="h", y=-0.2),
        )
        st.plotly_chart(h_fig, use_container_width=True)
