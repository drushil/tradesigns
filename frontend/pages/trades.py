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
    col_f1, col_f2, col_f3 = st.columns(3)
    with col_f1:
        tickers = ["All"] + sorted(df["ticker"].unique().tolist())
        sel_ticker = st.selectbox("Ticker", tickers)
    with col_f2:
        regimes = ["All"] + sorted(df["regime"].dropna().unique().tolist()) if "regime" in df.columns else ["All"]
        sel_regime = st.selectbox("Regime", regimes)
    with col_f3:
        outcomes = ["All", "Wins only", "Losses only"]
        sel_outcome = st.selectbox("Outcome", outcomes)

    fdf = df.copy()
    if sel_ticker != "All":
        fdf = fdf[fdf["ticker"] == sel_ticker]
    if sel_regime != "All" and "regime" in fdf.columns:
        fdf = fdf[fdf["regime"] == sel_regime]
    if sel_outcome == "Wins only":
        fdf = fdf[fdf["net_pnl_pct"] > 0]
    elif sel_outcome == "Losses only":
        fdf = fdf[fdf["net_pnl_pct"] <= 0]

    if sel_ticker != "All":
        profile_html = ticker_profile_html(sel_ticker, compact=True)
        if profile_html:
            st.markdown(profile_html, unsafe_allow_html=True)

    st.markdown("---")

    # ── Stats row ──────────────────────────────────────────────────────────
    wins  = fdf[fdf["net_pnl_pct"] > 0]
    losses= fdf[fdf["net_pnl_pct"] <= 0]

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Trades shown", len(fdf))
    c2.metric("Win rate", f"{len(wins)/len(fdf)*100:.1f}%" if len(fdf) else "—")
    c3.metric("Avg P&L", f"{fdf['net_pnl_pct'].mean():+.3f}%" if len(fdf) else "—")
    c4.metric("Total P&L", f"€{fdf['pnl_eur'].sum():+.2f}" if len(fdf) else "—")
    c5.metric("Avg hold", f"{fdf['hold_minutes'].mean():.0f} min" if "hold_minutes" in fdf.columns and len(fdf) else "—")

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
            st.plotly_chart(fig, use_container_width=True)

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
            st.plotly_chart(fig2, use_container_width=True)

    # ── Trade table ────────────────────────────────────────────────────────
    st.markdown("##### All Trades")
    display_cols = ["created_at", "ticker", "side", "net_pnl_pct",
                    "pnl_eur", "hold_minutes", "exit_reason", "regime",
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
        use_container_width=True,
        height=400,
    )
