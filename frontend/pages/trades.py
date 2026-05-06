"""frontend/pages/trades.py — Trade history and analysis."""
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd
from frontend.ticker_profiles import ticker_profile_html


def render():
    st.title("🔄 Trade History")

    try:
        from database.client import get_recent_trades
        trades = get_recent_trades(days=90)
    except Exception as e:
        st.error(f"DB error: {e}")
        return

    if not trades:
        st.info("No trades yet. The agent will populate this once it starts trading.")
        return

    df = pd.DataFrame(trades)
    df["created_at"] = pd.to_datetime(df.get("created_at", pd.Series()))
    df["net_pnl_pct"] = pd.to_numeric(df.get("net_pnl_pct", 0), errors="coerce").fillna(0)
    df["pnl_eur"]     = pd.to_numeric(df.get("pnl_eur", 0),     errors="coerce").fillna(0)

    # ── Filters ────────────────────────────────────────────────────────────
    col_f1, col_f2, col_f3, col_f4, col_f5 = st.columns(5)
    with col_f1:
        tickers = ["All"] + sorted(df["ticker"].unique().tolist())
        sel_ticker = st.selectbox("Ticker", tickers)
    with col_f2:
        regimes = ["All"] + sorted(df["regime"].dropna().unique().tolist()) if "regime" in df.columns else ["All"]
        sel_regime = st.selectbox("Regime", regimes)
    with col_f3:
        sides = ["All"] + sorted(df["side"].dropna().unique().tolist()) if "side" in df.columns else ["All"]
        sel_side = st.selectbox("Side", sides)
    with col_f4:
        outcomes = ["All", "Wins only", "Losses only"]
        sel_outcome = st.selectbox("Outcome", outcomes)
    with col_f5:
        exposures = ["All"] + sorted(df["exposure_direction"].dropna().unique().tolist()) if "exposure_direction" in df.columns else ["All"]
        sel_exposure = st.selectbox("Exposure", exposures)

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

    if sel_ticker != "All":
        profile_html = ticker_profile_html(sel_ticker, compact=True)
        if profile_html:
            st.markdown(profile_html, unsafe_allow_html=True)

    st.markdown("---")

    # ── Stats row ──────────────────────────────────────────────────────────
    wins  = fdf[fdf["net_pnl_pct"] > 0]
    losses= fdf[fdf["net_pnl_pct"] <= 0]

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Trades shown", len(fdf))
    c2.metric("Win rate", f"{len(wins)/len(fdf)*100:.1f}%" if len(fdf) else "—")
    c3.metric("Avg P&L", f"{fdf['net_pnl_pct'].mean():+.3f}%" if len(fdf) else "—")
    c4.metric("Total P&L", f"€{fdf['pnl_eur'].sum():+.2f}" if len(fdf) else "—")
    c5.metric("Avg hold", f"{fdf['hold_minutes'].mean():.0f} min" if "hold_minutes" in fdf.columns and len(fdf) else "—")
    bearish_count = int((fdf["exposure_direction"] == "short_market").sum()) if "exposure_direction" in fdf.columns else 0
    c6.metric("Bearish exposure", f"{bearish_count / len(fdf) * 100:.0f}%" if len(fdf) else "—")

    st.markdown("---")

    # ── P&L scatter ────────────────────────────────────────────────────────
    col_scatter, col_exit = st.columns(2)
    with col_scatter:
        st.markdown("##### P&L by Signal Score")
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
        st.markdown("##### Exit Reason Breakdown")
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
            st.markdown("##### P&L by Strategy")
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
            st.markdown("##### P&L by Exposure")
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

    # ── Trade table ────────────────────────────────────────────────────────
    st.markdown("##### All Trades")

    # Build hold-type badge column
    fdf = fdf.copy()
    def _swing_badge(row):
        is_swing = bool(row.get("promoted_to_swing") or row.get("swing_trade"))
        days     = row.get("hold_days_actual")
        if is_swing:
            return f"🚀 SWING {int(days)}d" if days else "🚀 SWING"
        return "⚡ INTRADAY"
    fdf["hold_type"] = fdf.apply(_swing_badge, axis=1)

    display_cols = ["created_at", "ticker", "side", "hold_type", "net_pnl_pct",
                    "pnl_eur", "hold_minutes", "exit_reason", "regime",
                    "exposure_direction", "strategy_family",
                    "composite_score", "llm_conviction"]
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
    )

    # ── Swing trade detail expanders ───────────────────────────────────────
    if "promoted_to_swing" in fdf.columns:
        swing_rows = fdf[fdf["promoted_to_swing"].fillna(False).astype(bool)]
        if not swing_rows.empty:
            st.markdown("##### Swing Trade Details")
            for _, row in swing_rows.sort_values("created_at", ascending=False).head(20).iterrows():
                label = (f"🚀 {row.get('ticker','?')} · "
                         f"{row.get('hold_days_actual','?')}d · "
                         f"{row.get('net_pnl_pct', 0):+.2f}%")
                with st.expander(label):
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Conviction", f"{float(row.get('swing_conviction') or 0):.0%}")
                    c2.metric("Hold days", row.get("hold_days_actual", "—"))
                    c3.metric("Daily re-evals", row.get("daily_reeval_count", 0))
                    reasons = row.get("swing_reasons") or []
                    if reasons:
                        st.markdown(f"**Reasons:** {', '.join(reasons)}")
                    if row.get("exit_trigger"):
                        st.markdown(f"**Exit trigger:** `{row['exit_trigger']}`")
