"""frontend/pages/grading.py — Setup grading + alpha leakage dashboard."""
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
from datetime import datetime, timezone, timedelta
from frontend.ui_help import column_config, help_text, info_label, metric, section_header
from frontend.ui_theme import page_header, status_pill


# ── Colour palette ────────────────────────────────────────────────────────────
_GRADE_COLOR = {"A+": "#00d4a0", "A": "#4fc3f7", "B": "#ffb74d", "C": "#ff5c5c"}
_GRADE_ORDER = ["A+", "A", "B", "C"]


def render():
    try:
        from database.client import get_client
        client = get_client()
    except Exception as e:
        st.error(f"DB error: {e}")
        return

    # ── Sidebar controls ──────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("### Grading filters")
        days = st.slider("Look-back (days)", 1, 30, 7, help=help_text("Look-back (days)"))
        min_fav = st.slider(
            "Alpha leakage threshold (%)",
            0.1, 2.0, 0.5, step=0.1,
            help=help_text("Alpha leakage threshold (%)"),
        )

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    page_header(
        "Setup Grading",
        "Adaptive setup quality, percentile baselines, and alpha leakage from blocked opportunities.",
        eyebrow="Quality Control",
        pills=[
            status_pill(f"{days}d look-back", "info"),
            status_pill(f"{min_fav:.1f}% leakage bar", "warning"),
        ],
    )

    # ── Load signals with grade ───────────────────────────────────────────────
    try:
        sig_resp = (
            client.table("signals")
            .select("ticker,created_at,composite_score,setup_grade,sector_confirmation,percentile_rank,orb_score,regime,action_hint")
            .gte("created_at", cutoff)
            .not_.is_("setup_grade", "null")
            .order("created_at", desc=True)
            .limit(2000)
            .execute()
        )
        sdf = pd.DataFrame(sig_resp.data or [])
    except Exception as e:
        sdf = pd.DataFrame()
        st.warning(f"Could not load signals: {e}")

    # ── Load trades with grade ────────────────────────────────────────────────
    try:
        tr_resp = (
            client.table("trades")
            .select("ticker,created_at,setup_grade,net_pnl_pct,pnl_eur,exit_reason,side,hold_minutes,composite_score,partial_exit_done")
            .gte("created_at", cutoff)
            .execute()
        )
        tdf = pd.DataFrame(tr_resp.data or [])
    except Exception as e:
        tdf = pd.DataFrame()

    # ── Load blocked opportunities ────────────────────────────────────────────
    try:
        blk_resp = (
            client.table("blocked_opportunities")
            .select("ticker,created_at,block_stage,block_reason,composite_score,max_favorable_pct,max_adverse_pct,setup_grade,a_plus_blocked,candidate_rank_score")
            .gte("created_at", cutoff)
            .execute()
        )
        bdf = pd.DataFrame(blk_resp.data or [])
    except Exception as e:
        bdf = pd.DataFrame()

    # ── Load percentile baselines ─────────────────────────────────────────────
    try:
        pct_resp = client.table("signal_percentiles").select("ticker,sample_count,p50,p70,p85,p90,p95,updated_at").execute()
        pdf = pd.DataFrame(pct_resp.data or [])
    except Exception as e:
        pdf = pd.DataFrame()

    # ── Numeric coercion ──────────────────────────────────────────────────────
    for df, cols in [
        (sdf, ["composite_score", "sector_confirmation", "percentile_rank", "orb_score"]),
        (tdf, ["net_pnl_pct", "pnl_eur", "hold_minutes", "composite_score"]),
        (bdf, ["composite_score", "max_favorable_pct", "max_adverse_pct", "candidate_rank_score"]),
        (pdf, ["sample_count", "p50", "p70", "p85", "p90", "p95"]),
    ]:
        for c in cols:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")

    # ══════════════════════════════════════════════════════════════════════════
    # ROW 1 — Top KPIs
    # ══════════════════════════════════════════════════════════════════════════
    k1, k2, k3, k4, k5, k6 = st.columns(6)

    total_graded = len(sdf) if not sdf.empty else 0
    a_plus_count = int((sdf["setup_grade"] == "A+").sum()) if not sdf.empty else 0
    a_count      = int((sdf["setup_grade"] == "A").sum())  if not sdf.empty else 0

    if not tdf.empty and "setup_grade" in tdf.columns:
        graded_trades = tdf[tdf["setup_grade"].notna()]
        a_plus_trades = graded_trades[graded_trades["setup_grade"] == "A+"]
        a_plus_win_rate = (a_plus_trades["net_pnl_pct"] > 0).mean() * 100 if len(a_plus_trades) else 0
        a_plus_avg_pnl  = a_plus_trades["net_pnl_pct"].mean() if len(a_plus_trades) else 0
    else:
        a_plus_win_rate = 0
        a_plus_avg_pnl  = 0

    pct_tickers = len(pdf) if not pdf.empty else 0
    leakage_count = int((bdf["max_favorable_pct"] >= min_fav).sum()) if not bdf.empty and "max_favorable_pct" in bdf.columns else 0

    metric(k1, "Graded signals", f"{total_graded:,}")
    metric(k2, "A+ setups", f"{a_plus_count:,}")
    metric(k3, "A setups", f"{a_count:,}")
    metric(k4, "A+ win rate", f"{a_plus_win_rate:.0f}%")
    metric(k5, "A+ avg P&L", f"{a_plus_avg_pnl:+.2f}%")
    metric(k6, "Missed alpha", f"{leakage_count}", help=f"Blocked ops with ≥{min_fav}% favorable move")

    st.markdown("---")

    # ══════════════════════════════════════════════════════════════════════════
    # ROW 2 — Grade distribution + P&L by grade
    # ══════════════════════════════════════════════════════════════════════════
    col_left, col_right = st.columns(2)

    with col_left:
        section_header("Grade distribution - signals", help_key="Count of recent computed signals by setup grade.")
        if not sdf.empty and "setup_grade" in sdf.columns:
            grade_counts = sdf["setup_grade"].value_counts().reindex(_GRADE_ORDER, fill_value=0).reset_index()
            grade_counts.columns = ["grade", "count"]
            fig = go.Figure(go.Bar(
                x=grade_counts["grade"],
                y=grade_counts["count"],
                marker_color=[_GRADE_COLOR.get(g, "#555") for g in grade_counts["grade"]],
                text=grade_counts["count"],
                textposition="outside",
            ))
            fig.update_layout(
                template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)", height=260,
                margin=dict(l=0, r=0, t=10, b=0),
                xaxis=dict(title=""), yaxis=dict(title="signals", gridcolor="#1a1a1a"),
                showlegend=False,
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No graded signals yet — agent will populate this once running.")

    with col_right:
        section_header("Avg P&L % by grade - closed trades", help_key="Average realised trade return grouped by entry setup grade.")
        if not tdf.empty and "setup_grade" in tdf.columns and "net_pnl_pct" in tdf.columns:
            graded = tdf[tdf["setup_grade"].isin(_GRADE_ORDER)].copy()
            if not graded.empty:
                by_grade = (
                    graded.groupby("setup_grade")["net_pnl_pct"]
                    .agg(avg="mean", count="count")
                    .reindex(_GRADE_ORDER)
                    .reset_index()
                    .dropna(subset=["avg"])
                )
                colors = [("#00d4a0" if v >= 0 else "#ff5c5c") for v in by_grade["avg"]]
                fig2 = go.Figure(go.Bar(
                    x=by_grade["setup_grade"],
                    y=by_grade["avg"].round(3),
                    marker_color=colors,
                    text=[f"{v:+.2f}%<br><span style='font-size:10px'>n={int(c)}</span>"
                          for v, c in zip(by_grade["avg"], by_grade["count"])],
                    textposition="outside",
                ))
                fig2.update_layout(
                    template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)", height=260,
                    margin=dict(l=0, r=0, t=10, b=0),
                    xaxis=dict(title=""), yaxis=dict(title="avg net P&L %", gridcolor="#1a1a1a"),
                    showlegend=False,
                )
                st.plotly_chart(fig2, use_container_width=True)
            else:
                st.info("No closed graded trades yet.")
        else:
            st.info("No graded trades yet.")

    # ══════════════════════════════════════════════════════════════════════════
    # ROW 3 — Grade over time + sector confirmation heatmap
    # ══════════════════════════════════════════════════════════════════════════
    col_a, col_b = st.columns(2)

    with col_a:
        section_header("A+ / A setups over time", help_key="Hourly count of high-grade setups in the selected look-back window.")
        if not sdf.empty and "created_at" in sdf.columns:
            sdf["ts"] = pd.to_datetime(sdf["created_at"], utc=True, errors="coerce")
            sdf["hour"] = sdf["ts"].dt.floor("H")
            top_grades = sdf[sdf["setup_grade"].isin(["A+", "A"])].copy()
            if not top_grades.empty:
                by_hour = top_grades.groupby(["hour", "setup_grade"]).size().reset_index(name="count")
                fig3 = px.bar(
                    by_hour, x="hour", y="count", color="setup_grade",
                    color_discrete_map=_GRADE_COLOR, barmode="stack",
                )
                fig3.update_layout(
                    template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)", height=260,
                    margin=dict(l=0, r=0, t=10, b=0),
                    xaxis=dict(title=""), yaxis=dict(title="count", gridcolor="#1a1a1a"),
                    legend=dict(orientation="h", y=1.1),
                )
                st.plotly_chart(fig3, use_container_width=True)
            else:
                st.info("No A+/A setups yet in this window.")
        else:
            st.info("No signal data yet.")

    with col_b:
        section_header("Sector confirmation by ticker (A+/A only)", help_key="Average confirmation from related sector or peer tickers for high-grade setups.")
        if not sdf.empty and "sector_confirmation" in sdf.columns:
            top = sdf[sdf["setup_grade"].isin(["A+", "A"])].copy()
            if not top.empty:
                by_ticker = (
                    top.groupby("ticker")["sector_confirmation"]
                    .agg(avg="mean", count="count")
                    .sort_values("avg", ascending=True)
                    .tail(15)
                    .reset_index()
                )
                bar_colors = ["#00d4a0" if v >= 0.67 else "#ffb74d" if v >= 0.5 else "#ff5c5c"
                              for v in by_ticker["avg"]]
                fig4 = go.Figure(go.Bar(
                    x=by_ticker["avg"].round(2),
                    y=by_ticker["ticker"],
                    orientation="h",
                    marker_color=bar_colors,
                    text=[f"{v:.0%}" for v in by_ticker["avg"]],
                    textposition="outside",
                ))
                fig4.add_vline(x=0.67, line_dash="dash", line_color="#555",
                               annotation_text="A+ threshold", annotation_position="top right")
                fig4.update_layout(
                    template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)", height=260,
                    margin=dict(l=0, r=0, t=10, b=0),
                    xaxis=dict(range=[0, 1.1], title="avg sector confirmation", gridcolor="#1a1a1a"),
                    yaxis=dict(title=""),
                    showlegend=False,
                )
                st.plotly_chart(fig4, use_container_width=True)
            else:
                st.info("No A+/A signals in this window.")
        else:
            st.info("No sector confirmation data yet.")

    # ══════════════════════════════════════════════════════════════════════════
    # ROW 4 — Adaptive percentile baselines
    # ══════════════════════════════════════════════════════════════════════════
    st.markdown("---")
    section_header("Adaptive percentile baselines - composite score windows", help_key="Per-ticker rolling composite-score thresholds used to decide whether a setup is unusually strong.")

    if not pdf.empty:
        display_pdf = pdf.copy()
        for col in ["p50", "p70", "p85", "p90", "p95"]:
            if col in display_pdf.columns:
                display_pdf[col] = display_pdf[col].apply(lambda x: f"{x:.3f}" if pd.notna(x) else "—")
        display_pdf["sample_count"] = display_pdf["sample_count"].fillna(0).astype(int)
        if "updated_at" in display_pdf.columns:
            display_pdf["updated_at"] = pd.to_datetime(display_pdf["updated_at"], errors="coerce").dt.strftime("%H:%M")

        display_pdf = display_pdf[["ticker", "sample_count", "p50", "p70", "p85", "p90", "p95", "updated_at"]].rename(columns={
            "ticker": "Ticker", "sample_count": "Samples",
            "updated_at": "Last updated",
        })

        def _row_style(row):
            samples = int(str(row["Samples"]).replace(",", "") or 0)
            color = "#222" if samples < 20 else "transparent"
            return [f"background-color: {color}"] * len(row)

        st.dataframe(
            display_pdf.style.apply(_row_style, axis=1),
            use_container_width=True, hide_index=True, height=220,
            column_config=column_config(display_pdf.columns),
        )
        st.caption("Grey rows: < 20 samples — cold-start mode (uses fixed threshold instead of percentile).")
    else:
        st.info("No percentile data yet. Will populate after the first few signal cycles.")

    # ══════════════════════════════════════════════════════════════════════════
    # ROW 5 — Alpha leakage analysis
    # ══════════════════════════════════════════════════════════════════════════
    st.markdown("---")
    section_header("Alpha leakage - blocked opportunities", help_key="Alpha leakage — blocked opportunities")

    if not bdf.empty and "max_favorable_pct" in bdf.columns:
        replayed = bdf[bdf["max_favorable_pct"].notna()].copy()

        c1, c2, c3, c4 = st.columns(4)
        total_blocked = len(bdf)
        total_replayed = len(replayed)
        profitable = (replayed["max_favorable_pct"] >= min_fav).sum()
        hit_rate = profitable / len(replayed) * 100 if len(replayed) else 0

        metric(c1, "Total blocked", f"{total_blocked:,}")
        metric(c2, "Replayed", f"{total_replayed:,}")
        metric(c3, f"Profitable if taken (≥{min_fav}%)", f"{int(profitable)}", help_key="Profitable if taken")
        metric(c4, "Hit rate", f"{hit_rate:.0f}%")

        if not replayed.empty:
            col_leak1, col_leak2 = st.columns(2)

            with col_leak1:
                st.markdown(f"**{info_label('By block stage', 'Replay outcome grouped by the gate or decision stage that blocked the trade.')}**", unsafe_allow_html=True)
                stage_summary = (
                    replayed.groupby("block_stage")
                    .agg(
                        total=("max_favorable_pct", "count"),
                        profitable=("max_favorable_pct", lambda x: (x >= min_fav).sum()),
                        avg_fav=("max_favorable_pct", "mean"),
                    )
                    .reset_index()
                    .sort_values("profitable", ascending=False)
                )
                stage_summary["hit_rate"] = (stage_summary["profitable"] / stage_summary["total"] * 100).round(0).astype(int).astype(str) + "%"
                stage_summary["avg_fav"] = stage_summary["avg_fav"].apply(lambda x: f"{x:+.2f}%")
                st.dataframe(
                    stage_summary[["block_stage", "total", "profitable", "hit_rate", "avg_fav"]].rename(columns={
                        "block_stage": "Stage", "total": "Blocks",
                        "profitable": "Profitable", "hit_rate": "Hit rate", "avg_fav": "Avg fav move",
                    }),
                    use_container_width=True, hide_index=True,
                    column_config=column_config(["Stage", "Blocks", "Profitable", "Hit rate", "Avg fav move"]),
                )

            with col_leak2:
                st.markdown(f"**{info_label('Favorable move distribution (replayed)', 'Distribution of maximum favorable moves after blocked candidates.')}**", unsafe_allow_html=True)
                fig5 = go.Figure()
                missed = replayed[replayed["max_favorable_pct"] >= min_fav]["max_favorable_pct"]
                correct = replayed[replayed["max_favorable_pct"] < min_fav]["max_favorable_pct"]
                if not missed.empty:
                    fig5.add_trace(go.Histogram(x=missed, name="Profitable if taken",
                                                marker_color="#00d4a0", opacity=0.7, nbinsx=20))
                if not correct.empty:
                    fig5.add_trace(go.Histogram(x=correct, name="Correctly blocked",
                                                marker_color="#ff5c5c", opacity=0.7, nbinsx=20))
                fig5.update_layout(
                    template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)", height=220, barmode="overlay",
                    margin=dict(l=0, r=0, t=10, b=0),
                    xaxis=dict(title="max favorable move %", gridcolor="#1a1a1a"),
                    yaxis=dict(title="count", gridcolor="#1a1a1a"),
                    legend=dict(orientation="h", y=1.1, font=dict(size=11)),
                )
                st.plotly_chart(fig5, use_container_width=True)

            # Top missed opportunities table
            st.markdown(f"**{info_label('Top missed opportunities', 'Blocked candidates with the largest later favorable move.')}**", unsafe_allow_html=True)
            top_missed = (
                replayed[replayed["max_favorable_pct"] >= min_fav]
                .sort_values("max_favorable_pct", ascending=False)
                .head(10)
            )
            if not top_missed.empty:
                display_cols = ["ticker", "block_stage", "block_reason", "setup_grade",
                                "composite_score", "max_favorable_pct", "max_adverse_pct"]
                display_cols = [c for c in display_cols if c in top_missed.columns]
                fmt = top_missed[display_cols].copy()
                for col in ["composite_score", "max_favorable_pct", "max_adverse_pct"]:
                    if col in fmt.columns:
                        fmt[col] = fmt[col].apply(lambda x: f"{x:+.3f}" if pd.notna(x) else "—")
                missed_display = fmt.rename(columns={
                    "ticker": "Ticker", "block_stage": "Stage", "block_reason": "Reason",
                    "setup_grade": "Grade", "composite_score": "Composite",
                    "max_favorable_pct": "Max fav %", "max_adverse_pct": "Max adverse %",
                })
                st.dataframe(
                    missed_display,
                    use_container_width=True,
                    hide_index=True,
                    column_config=column_config(missed_display.columns),
                )
    else:
        st.info("No replayed blocked opportunities yet. They populate ~20 min after each block.")

    # ══════════════════════════════════════════════════════════════════════════
    # ROW 6 — Partial exits tracker
    # ══════════════════════════════════════════════════════════════════════════
    if not tdf.empty and "partial_exit_done" in tdf.columns:
        partials = tdf[tdf["partial_exit_done"].astype(str).str.lower().isin({"true", "1", "t"})]
        if not partials.empty:
            st.markdown("---")
            section_header("Partial exits + runners", help_key="Closed trades where the strategy took partial profits and left a runner position.")
            pcols = ["ticker", "setup_grade", "side", "net_pnl_pct", "hold_minutes", "exit_reason"]
            pcols = [c for c in pcols if c in partials.columns]
            pdisplay = partials[pcols].copy()
            if "net_pnl_pct" in pdisplay.columns:
                pdisplay["net_pnl_pct"] = pdisplay["net_pnl_pct"].apply(lambda x: f"{x:+.2f}%" if pd.notna(x) else "—")
            partial_display = pdisplay.rename(columns={
                "ticker": "Ticker", "setup_grade": "Grade", "side": "Side",
                "net_pnl_pct": "Net P&L", "hold_minutes": "Hold (min)", "exit_reason": "Exit reason",
            })
            st.dataframe(
                partial_display,
                use_container_width=True,
                hide_index=True,
                column_config=column_config(partial_display.columns),
            )
