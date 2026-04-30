"""frontend/pages/overview.py — Portfolio overview dashboard."""
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
from datetime import datetime


def render():
    st.title("📊 Portfolio Overview")

    # ── Load data ──────────────────────────────────────────────────────────
    try:
        from database.client import get_snapshots, get_trade_stats
        from backend.broker.alpaca import get_account, get_positions
        snapshots  = get_snapshots(days=30)
        trade_stats = get_trade_stats(days=30)
        account    = get_account()
        positions  = get_positions()
    except Exception as e:
        st.error(f"Could not connect to data sources: {e}")
        st.info("Make sure your .env file is configured and Supabase is set up.")
        _render_demo()
        return

    import os
    equity  = account.get("portfolio_value", 0)
    cash    = account.get("cash", 0)
    start   = float(os.getenv("STARTING_CAPITAL_EUR", "100"))
    ceiling = account.get("capital_ceiling_eur")
    alpaca_actual = account.get("alpaca_actual")
    # P&L base: convert EUR start to USD when ceiling is active, else use raw equity
    start_base = start * 1.08 if ceiling else start
    cum_pnl_pct = (equity - start_base) / start_base * 100 if start_base > 0 else 0
    try:
        from backend.signals.engine import detect_regime
        regime_state = detect_regime()
    except Exception:
        regime_state = None

    # ── KPI row ────────────────────────────────────────────────────────────
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Portfolio Value", f"€{equity:,.2f}",
              delta=f"{cum_pnl_pct:+.2f}% all-time")
    c2.metric("Cash Available", f"€{cash:,.2f}",
              delta=f"{cash/equity*100:.0f}% of portfolio" if equity else None)
    c3.metric("Total Trades", trade_stats.get("total", 0))
    c4.metric("Win Rate",
              f"{trade_stats.get('win_rate', 0):.1f}%",
              delta=f"W:{trade_stats.get('wins',0)} L:{trade_stats.get('losses',0)}")
    c5.metric("Total P&L",
              f"€{trade_stats.get('total_pnl_eur', 0):+.2f}",
              delta=f"avg {trade_stats.get('avg_pnl',0):+.3f}%/trade")

    if ceiling and alpaca_actual:
        st.caption(
            f"Capital ceiling active: agent uses €{ceiling:,.0f} "
            f"(≈ ${ceiling * 1.08:,.0f}) · Alpaca account holds ${alpaca_actual:,.0f}"
        )

    if regime_state:
        label = regime_state.market_regime.upper()
        icon = "🐂" if label == "BULL" else ("🐻" if label == "BEAR" else "↔")
        st.markdown(f"""
        <div style="margin:12px 0 4px;padding:12px 14px;border:1px solid #222;border-radius:8px;background:#101010">
          <span style="font-size:22px;font-weight:700">{icon} {label}</span>
          <span style="margin-left:16px;color:#aaa">VIX {regime_state.vix:.1f}</span>
          <span style="margin-left:16px;color:#777">SPY vs SMA200 {regime_state.price_vs_sma200_pct:+.2f}%</span>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("---")

    # ── Equity curve ───────────────────────────────────────────────────────
    col_chart, col_pos = st.columns([2, 1])

    with col_chart:
        st.markdown("##### Equity Curve")
        if snapshots:
            df = pd.DataFrame(snapshots)
            df["snapshot_at"] = pd.to_datetime(df["snapshot_at"])
            df = df.sort_values("snapshot_at")

            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=df["snapshot_at"],
                y=df["total_value_eur"],
                mode="lines",
                fill="tozeroy",
                line=dict(color="#00d4a0", width=2),
                fillcolor="rgba(0,212,160,0.08)",
                name="Portfolio Value",
            ))
            fig.add_hline(y=start, line_dash="dot",
                          line_color="#555", annotation_text="Start")
            fig.update_layout(
                template="plotly_dark",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                height=280,
                margin=dict(l=0, r=0, t=10, b=0),
                showlegend=False,
                yaxis=dict(gridcolor="#1a1a1a"),
                xaxis=dict(gridcolor="#1a1a1a"),
            )
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("No portfolio history yet. Start the agent to begin trading.")

    with col_pos:
        st.markdown("##### Open Positions")
        if positions:
            for p in positions:
                pnl_color = "positive" if p["unrealized_pl"] >= 0 else "negative"
                st.markdown(f"""
                <div class="signal-card">
                    <div class="signal-name">{p['ticker']}</div>
                    <div style="font-family:'DM Mono',monospace;font-size:14px">
                        {p['qty']:.4f} @ €{p['avg_entry']:.2f}
                    </div>
                    <div class="signal-score {pnl_color}" style="font-size:18px">
                        €{p['unrealized_pl']:+.2f} ({p['unrealized_plpc']:+.2f}%)
                    </div>
                </div>""", unsafe_allow_html=True)
        else:
            st.info("No open positions")

    # ── Daily P&L bar chart ────────────────────────────────────────────────
    if snapshots and len(snapshots) > 1:
        st.markdown("##### Daily P&L")
        df = pd.DataFrame(snapshots)
        df["snapshot_at"] = pd.to_datetime(df["snapshot_at"])
        df = df.sort_values("snapshot_at")
        df["color"] = df["daily_pnl_pct"].apply(lambda x: "#00d4a0" if x >= 0 else "#ff5c5c")

        fig2 = go.Figure(go.Bar(
            x=df["snapshot_at"], y=df["daily_pnl_pct"],
            marker_color=df["color"], name="Daily P&L %"
        ))
        fig2.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=180, margin=dict(l=0, r=0, t=10, b=0),
            yaxis=dict(gridcolor="#1a1a1a", ticksuffix="%"),
            xaxis=dict(gridcolor="#1a1a1a"),
        )
        st.plotly_chart(fig2, width="stretch")

    # ── Regime & signal breakdown ──────────────────────────────────────────
    from database.client import get_recent_trades
    trades = get_recent_trades(days=30)
    if trades:
        st.markdown("---")
        st.markdown("##### Performance by Market Regime")
        df_t = pd.DataFrame(trades)
        if "regime" in df_t.columns and "net_pnl_pct" in df_t.columns:
            regime_stats = (df_t.groupby("regime")["net_pnl_pct"]
                           .agg(["mean", "count"])
                           .reset_index()
                           .rename(columns={"mean": "avg_pnl", "count": "trades"}))
            cols = st.columns(len(regime_stats))
            for i, row in regime_stats.iterrows():
                color = "positive" if row["avg_pnl"] > 0 else "negative"
                cols[i].markdown(f"""
                <div class="signal-card">
                    <div class="signal-name">{row['regime']}</div>
                    <div class="signal-score {color}" style="font-size:20px">
                        {row['avg_pnl']:+.3f}%
                    </div>
                    <div style="font-size:11px;color:#555">{row['trades']:.0f} trades</div>
                </div>""", unsafe_allow_html=True)


def _render_demo():
    """Show a demo/placeholder when DB not connected."""
    st.warning("Running in demo mode — connect your .env to see live data")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Portfolio Value", "€100.00", delta="paper mode")
    c2.metric("Cash Available", "€100.00", delta="100%")
    c3.metric("Total Trades", "0")
    c4.metric("Win Rate", "—")
    c5.metric("Total P&L", "€0.00")
