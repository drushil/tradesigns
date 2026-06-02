"""frontend/pages/trades.py — Trade history and analysis."""
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd
from frontend.ticker_profiles import ticker_profile_html
from frontend.ui_help import column_config, metric, section_title, selectbox
from frontend.ui_theme import page_header, status_pill


def render():
    try:
        from database.client import get_recent_trades
        trades = get_recent_trades(days=90, source="agent")
    except Exception as e:
        st.error(f"DB error: {e}")
        return

    if not trades:
        page_header(
            "Trade History",
            "Closed trade execution, P&L, replay, and swing-vs-intraday analysis.",
            eyebrow="Execution Review",
        )
        st.info("No automated trades yet. The agent will populate this once it starts trading.")
        st.caption("Advisory-based manual trades (Trade Republic) are tracked on the Advisory page.")
        return

    df = pd.DataFrame(trades)
    df["created_at"] = pd.to_datetime(df.get("created_at", pd.Series()))
    df["net_pnl_pct"] = pd.to_numeric(df.get("net_pnl_pct", 0), errors="coerce").fillna(0)
    df["pnl_eur"]     = pd.to_numeric(df.get("pnl_eur", 0),     errors="coerce").fillna(0)
    if "hold_minutes" in df.columns:
        df["hold_minutes"] = pd.to_numeric(df["hold_minutes"], errors="coerce").fillna(0)
    if "hold_days_actual" in df.columns:
        df["hold_days_actual"] = pd.to_numeric(df["hold_days_actual"], errors="coerce")
    if "swing_conviction" in df.columns:
        df["swing_conviction"] = pd.to_numeric(df["swing_conviction"], errors="coerce")
    for col in [
        "post_exit_max_favorable_pct",
        "post_exit_max_adverse_pct",
        "post_exit_close_after_pct",
    ]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    def _truthy(value) -> bool:
        if pd.isna(value):
            return False
        if isinstance(value, str):
            return value.strip().lower() in {"true", "t", "1", "yes", "y"}
        return bool(value)

    swing_exit_reasons = {
        "chandelier_stop", "swing_exit", "earnings_tomorrow",
        "regime_turned_bear", "momentum_reversed", "take_profit_8pct",
    }

    def _is_swing_trade(row) -> bool:
        return (
            _truthy(row.get("promoted_to_swing"))
            or _truthy(row.get("swing_trade"))
            or row.get("horizon") == "swing"
            or row.get("exit_reason") in swing_exit_reasons
        )

    df["is_swing"] = df.apply(_is_swing_trade, axis=1)
    df["hold_type_filter"] = df["is_swing"].map({True: "Swing", False: "Intraday"})

    page_header(
        "Trade History",
        "Closed trade execution, P&L, replay, and swing-vs-intraday analysis.",
        eyebrow="Execution Review",
        pills=[
            status_pill(f"{len(df)} trades", "info"),
            status_pill(f"{int(df['is_swing'].sum())} swings", "warning" if df["is_swing"].any() else "neutral"),
            status_pill(f"€{df['pnl_eur'].sum():+.2f}", "positive" if df["pnl_eur"].sum() >= 0 else "negative"),
        ],
    )

    st.caption("Automated agent trades only. Advisory-based manual trades are tracked on the Advisory page.")

    # ── Filters ────────────────────────────────────────────────────────────
    col_f1, col_f2, col_f3, col_f4, col_f5, col_f6 = st.columns(6)
    with col_f1:
        tickers = ["All"] + sorted(df["ticker"].unique().tolist())
        sel_ticker = selectbox("Ticker", tickers)
    with col_f2:
        regimes = ["All"] + sorted(df["regime"].dropna().unique().tolist()) if "regime" in df.columns else ["All"]
        sel_regime = selectbox("Regime", regimes)
    with col_f3:
        sides = ["All"] + sorted(df["side"].dropna().unique().tolist()) if "side" in df.columns else ["All"]
        sel_side = selectbox("Side", sides)
    with col_f4:
        outcomes = ["All", "Wins only", "Losses only"]
        sel_outcome = selectbox("Outcome", outcomes)
    with col_f5:
        exposures = ["All"] + sorted(df["exposure_direction"].dropna().unique().tolist()) if "exposure_direction" in df.columns else ["All"]
        sel_exposure = selectbox("Exposure", exposures)
    with col_f6:
        sel_hold_type = selectbox("Hold type", ["All", "Intraday", "Swing"])

    fdf = df.copy()
    if sel_ticker != "All":
        fdf = fdf[fdf["ticker"] == sel_ticker]
    if sel_regime != "All" and "regime" in fdf.columns:
        fdf = fdf[fdf["regime"] == sel_regime]
    if sel_side != "All" and "side" in fdf.columns:
        fdf = fdf[fdf["side"] == sel_side]
    if sel_outcome == "Wins only":
        fdf = fdf[fdf["net_pnl_pct"] > 0]
    elif sel_outcome == "Losses only":
        fdf = fdf[fdf["net_pnl_pct"] <= 0]
    if sel_exposure != "All" and "exposure_direction" in fdf.columns:
        fdf = fdf[fdf["exposure_direction"] == sel_exposure]
    if sel_hold_type != "All":
        fdf = fdf[fdf["hold_type_filter"] == sel_hold_type]

    if sel_ticker != "All":
        profile_html = ticker_profile_html(sel_ticker, compact=True)
        if profile_html:
            st.markdown(profile_html, unsafe_allow_html=True)

    st.markdown("---")

    # ── Stats row ──────────────────────────────────────────────────────────
    wins  = fdf[fdf["net_pnl_pct"] > 0]
    losses= fdf[fdf["net_pnl_pct"] <= 0]

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    metric(c1, "Trades shown", len(fdf))
    metric(c2, "Win rate", f"{len(wins)/len(fdf)*100:.1f}%" if len(fdf) else "—")
    metric(c3, "Avg P&L", f"{fdf['net_pnl_pct'].mean():+.3f}%" if len(fdf) else "—")
    metric(c4, "Total P&L", f"€{fdf['pnl_eur'].sum():+.2f}" if len(fdf) else "—")
    metric(c5, "Avg hold", f"{fdf['hold_minutes'].mean():.0f} min" if "hold_minutes" in fdf.columns and len(fdf) else "—")
    bearish_count = int((fdf["exposure_direction"] == "short_market").sum()) if "exposure_direction" in fdf.columns else 0
    metric(c6, "Bearish exposure", f"{bearish_count / len(fdf) * 100:.0f}%" if len(fdf) else "—")

    st.markdown("---")

    # ── Swing stats ─────────────────────────────────────────────────────────
    section_title("Swing vs Intraday")
    swing_df = fdf[fdf["is_swing"]]
    intraday_df = fdf[~fdf["is_swing"]]

    def _win_rate(data):
        return (data["net_pnl_pct"].gt(0).mean() * 100) if len(data) else None

    def _avg_hold_label(data):
        if not len(data) or "hold_minutes" not in data.columns:
            return "—"
        avg_minutes = data["hold_minutes"].mean()
        if avg_minutes >= 390:
            return f"{avg_minutes / 390:.1f} d"
        return f"{avg_minutes:.0f} min"

    sc1, sc2, sc3, sc4, sc5, sc6 = st.columns(6)
    metric(sc1, "Swing trades", len(swing_df))
    metric(sc2, "Swing win rate", f"{_win_rate(swing_df):.1f}%" if len(swing_df) else "—")
    metric(sc3, "Swing avg P&L", f"{swing_df['net_pnl_pct'].mean():+.3f}%" if len(swing_df) else "—")
    metric(sc4, "Swing total P&L", f"€{swing_df['pnl_eur'].sum():+.2f}" if len(swing_df) else "—")
    metric(sc5, "Swing avg hold", _avg_hold_label(swing_df))
    metric(
        sc6,
        "Avg conviction",
        f"{swing_df['swing_conviction'].dropna().mean():.0%}"
        if "swing_conviction" in swing_df.columns and swing_df["swing_conviction"].notna().any()
        else "—",
    )

    compare_rows = []
    for label, data in [("Intraday", intraday_df), ("Swing", swing_df)]:
        if len(data):
            compare_rows.append({
                "Hold Type": label,
                "Trades": len(data),
                "Avg Net P&L (%)": data["net_pnl_pct"].mean(),
                "Win Rate (%)": _win_rate(data),
            })
    if compare_rows:
        compare_df = pd.DataFrame(compare_rows)
        col_cmp1, col_cmp2 = st.columns(2)
        with col_cmp1:
            fig_cmp = px.bar(
                compare_df, x="Hold Type", y="Avg Net P&L (%)", color="Hold Type",
                text=compare_df["Avg Net P&L (%)"].map(lambda v: f"{v:+.3f}%"),
                template="plotly_dark",
                color_discrete_map={"Intraday": "#888", "Swing": "#ffd166"},
            )
            fig_cmp.add_hline(y=0, line_dash="dot", line_color="#555")
            fig_cmp.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                                  plot_bgcolor="rgba(0,0,0,0)", height=240,
                                  margin=dict(l=0, r=0, t=10, b=0),
                                  showlegend=False)
            st.plotly_chart(fig_cmp, width="stretch")
        with col_cmp2:
            comparison_display = compare_df.assign(
                **{
                    "Avg Net P&L (%)": compare_df["Avg Net P&L (%)"].map(lambda v: f"{v:+.3f}%"),
                    "Win Rate (%)": compare_df["Win Rate (%)"].map(lambda v: f"{v:.1f}%"),
                }
            )
            st.dataframe(
                comparison_display,
                width="stretch",
                hide_index=True,
                column_config=column_config(comparison_display.columns),
            )
    else:
        st.info("No closed trades match the current filters yet.")

    st.markdown("---")

    # ── P&L scatter ────────────────────────────────────────────────────────
    col_scatter, col_exit = st.columns(2)
    with col_scatter:
        section_title("P&L by Signal Score")
        if "composite_score" in fdf.columns:
            fig = px.scatter(
                fdf, x="composite_score", y="net_pnl_pct",
                color="ticker", size=fdf["net_pnl_pct"].abs().clip(lower=0.01),
                color_discrete_sequence=px.colors.qualitative.Prism,
                labels={"composite_score": "Signal Score", "net_pnl_pct": "Net P&L (%)"},
                template="plotly_dark",
            )
            fig.add_hline(y=0, line_dash="dot", line_color="#555")
            fig.add_vline(x=0, line_dash="dot", line_color="#555")
            fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                              plot_bgcolor="rgba(0,0,0,0)", height=300,
                              margin=dict(l=0,r=0,t=10,b=0))
            st.plotly_chart(fig, width="stretch")

    with col_exit:
        section_title("Exit Reason Breakdown")
        if "exit_reason" in fdf.columns:
            exit_counts = fdf["exit_reason"].value_counts().reset_index()
            exit_counts.columns = ["reason", "count"]
            fig2 = px.pie(exit_counts, values="count", names="reason",
                          template="plotly_dark",
                          color_discrete_sequence=["#00d4a0", "#ff5c5c", "#ffd166", "#888"])
            fig2.update_layout(paper_bgcolor="rgba(0,0,0,0)", height=300,
                               margin=dict(l=0,r=0,t=10,b=0))
            st.plotly_chart(fig2, width="stretch")

    if "strategy_family" in fdf.columns and "exposure_direction" in fdf.columns:
        col_strategy, col_exposure = st.columns(2)
        with col_strategy:
            section_title("P&L by Strategy")
            strategy_perf = (fdf.groupby("strategy_family", dropna=False)
                               .agg(trades=("ticker", "count"), avg_pnl=("net_pnl_pct", "mean"))
                               .reset_index())
            fig3 = px.bar(
                strategy_perf, x="strategy_family", y="avg_pnl", color="trades",
                template="plotly_dark",
                labels={"strategy_family": "Strategy", "avg_pnl": "Avg Net P&L (%)"},
            )
            fig3.add_hline(y=0, line_dash="dot", line_color="#555")
            fig3.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                               plot_bgcolor="rgba(0,0,0,0)", height=280,
                               margin=dict(l=0,r=0,t=10,b=0))
            st.plotly_chart(fig3, width="stretch")
        with col_exposure:
            section_title("P&L by Exposure")
            exposure_perf = (fdf.groupby("exposure_direction", dropna=False)
                               .agg(trades=("ticker", "count"), avg_pnl=("net_pnl_pct", "mean"))
                               .reset_index())
            fig4 = px.bar(
                exposure_perf, x="exposure_direction", y="avg_pnl", color="trades",
                template="plotly_dark",
                labels={"exposure_direction": "Exposure", "avg_pnl": "Avg Net P&L (%)"},
            )
            fig4.add_hline(y=0, line_dash="dot", line_color="#555")
            fig4.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                               plot_bgcolor="rgba(0,0,0,0)", height=280,
                               margin=dict(l=0,r=0,t=10,b=0))
            st.plotly_chart(fig4, width="stretch")

    # ── Post-exit replay ────────────────────────────────────────────────────
    replay_cols = {
        "post_exit_max_favorable_pct",
        "post_exit_max_adverse_pct",
        "post_exit_close_after_pct",
    }
    if replay_cols.issubset(set(fdf.columns)):
        replay_df = fdf[fdf["post_exit_max_favorable_pct"].notna()].copy()
        if not replay_df.empty:
            st.markdown("---")
            section_title("Post-Exit Replay")
            left_money = replay_df[replay_df["post_exit_max_favorable_pct"] >= 0.5]

            rc1, rc2, rc3, rc4 = st.columns(4)
            metric(rc1, "Replayed exits", len(replay_df))
            metric(rc2, "Avg missed upside", f"{replay_df['post_exit_max_favorable_pct'].mean():+.2f}%")
            metric(rc3, "Avg adverse avoided", f"{replay_df['post_exit_max_adverse_pct'].mean():+.2f}%")
            metric(rc4, "Left money count", len(left_money))

            col_reason, col_table = st.columns(2)
            with col_reason:
                by_exit = (
                    replay_df.groupby("exit_reason", dropna=False)
                    .agg(
                        trades=("ticker", "count"),
                        avg_missed=("post_exit_max_favorable_pct", "mean"),
                        avg_close_after=("post_exit_close_after_pct", "mean"),
                    )
                    .reset_index()
                    .sort_values("avg_missed", ascending=False)
                )
                fig_replay = px.bar(
                    by_exit,
                    x="exit_reason",
                    y="avg_missed",
                    color="trades",
                    template="plotly_dark",
                    labels={
                        "exit_reason": "Exit reason",
                        "avg_missed": "Avg post-exit favorable move (%)",
                    },
                )
                fig_replay.add_hline(y=0, line_dash="dot", line_color="#555")
                fig_replay.update_layout(
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    height=280,
                    margin=dict(l=0, r=0, t=10, b=0),
                )
                st.plotly_chart(fig_replay, width="stretch")

            with col_table:
                top_left = (
                    replay_df.sort_values("post_exit_max_favorable_pct", ascending=False)
                    .head(12)
                )
                cols = [
                    "created_at", "ticker", "exit_reason", "net_pnl_pct",
                    "post_exit_max_favorable_pct", "post_exit_max_adverse_pct",
                    "post_exit_close_after_pct",
                ]
                cols = [c for c in cols if c in top_left.columns]
                st.dataframe(
                    top_left[cols],
                    width="stretch",
                    hide_index=True,
                    column_config=column_config(cols),
                )

    # ── Trade table ────────────────────────────────────────────────────────
    section_title("All Trades")

    # Build hold-type badge column
    fdf = fdf.copy()
    def _swing_badge(row):
        days     = row.get("hold_days_actual")
        if row.get("is_swing"):
            return f"SWING {int(days)}d" if pd.notna(days) and days else "SWING"
        return "⚡ INTRADAY"
    fdf["hold_type"] = fdf.apply(_swing_badge, axis=1)

    display_cols = ["created_at", "ticker", "side", "hold_type", "net_pnl_pct",
                    "pnl_eur", "hold_minutes", "exit_reason", "regime",
                    "exposure_direction", "strategy_family",
                    "composite_score", "llm_conviction",
                    "post_exit_max_favorable_pct", "post_exit_close_after_pct"]
    available = [c for c in display_cols if c in fdf.columns]
    show_df = fdf[available].sort_values("created_at", ascending=False).head(100)

    # Colour-code P&L column
    def highlight_pnl(val):
        if isinstance(val, float):
            color = "color: #00d4a0" if val > 0 else ("color: #ff5c5c" if val < 0 else "")
            return color
        return ""

    st.dataframe(
        show_df.style.map(highlight_pnl, subset=["net_pnl_pct"] if "net_pnl_pct" in show_df.columns else []),
        width="stretch",
        height=400,
        column_config=column_config(show_df.columns),
    )

    # ── Swing trade detail expanders ───────────────────────────────────────
    if "is_swing" in fdf.columns:
        swing_rows = fdf[fdf["is_swing"]]
        if not swing_rows.empty:
            section_title("Swing Trade Details")
            for _, row in swing_rows.sort_values("created_at", ascending=False).head(20).iterrows():
                label = (f"🚀 {row.get('ticker','?')} · "
                         f"{row.get('hold_days_actual','?')}d · "
                         f"{row.get('net_pnl_pct', 0):+.2f}%")
                with st.expander(label):
                    c1, c2, c3 = st.columns(3)
                    metric(c1, "Conviction", f"{float(row.get('swing_conviction') or 0):.0%}")
                    metric(c2, "Hold days", row.get("hold_days_actual", "—"))
                    metric(c3, "Daily re-evals", row.get("daily_reeval_count", 0))
                    reasons = row.get("swing_reasons") or []
                    if reasons:
                        st.markdown(f"**Reasons:** {', '.join(reasons)}")
                    if row.get("exit_trigger"):
                        st.markdown(f"**Exit trigger:** `{row['exit_trigger']}`")
