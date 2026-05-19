"""frontend/pages/overview.py — Portfolio overview dashboard."""
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
from datetime import datetime
from frontend.ui_help import metric, section_title


def _safe_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


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
    latest_snapshot = snapshots[0] if snapshots else {}
    account_error = account.get("error") if isinstance(account, dict) else None
    start         = _safe_float(os.getenv("STARTING_CAPITAL_EUR", "3000"), 3000.0)
    fx_rate       = _safe_float(os.getenv("EURUSD_RATE", "1.08"), 1.08) or 1.08
    ceiling       = account.get("capital_ceiling_eur") if isinstance(account, dict) else None
    alpaca_actual = account.get("alpaca_actual_usd") if isinstance(account, dict) else None

    if account_error:
        equity_eur = _safe_float(latest_snapshot.get("total_value_eur"))
        cash_eur = _safe_float(latest_snapshot.get("cash_eur"))
        equity_usd = _safe_float(
            latest_snapshot.get("effective_equity_usd")
            or latest_snapshot.get("broker_equity_usd")
            or equity_eur * fx_rate
        )
        cash_usd = _safe_float(
            latest_snapshot.get("effective_cash_usd")
            or latest_snapshot.get("broker_cash_usd")
            or cash_eur * fx_rate
        )
    else:
        equity_usd = _safe_float(account.get("portfolio_value") or account.get("equity"))
        cash_usd = _safe_float(account.get("cash"))
        equity_eur = equity_usd / fx_rate if fx_rate else equity_usd
        cash_eur = cash_usd / fx_rate if fx_rate else cash_usd

        if equity_eur <= 0 and _safe_float(latest_snapshot.get("total_value_eur")) > 0:
            equity_eur = _safe_float(latest_snapshot.get("total_value_eur"))
            cash_eur = _safe_float(latest_snapshot.get("cash_eur"))
            equity_usd = _safe_float(latest_snapshot.get("effective_equity_usd") or equity_eur * fx_rate)
            cash_usd = _safe_float(latest_snapshot.get("effective_cash_usd") or cash_eur * fx_rate)

    if account_error:
        st.warning(
            "Live Alpaca account data is unavailable, so Overview is showing the latest stored "
            f"portfolio snapshot. Broker error: {str(account_error)[:180]}"
        )

    if positions and isinstance(positions[0], dict) and positions[0].get("error"):
        st.warning(f"Live Alpaca positions unavailable: {str(positions[0].get('error'))[:180]}")
        positions = []

    cum_pnl_pct   = (equity_eur - start) / start * 100 if start > 0 else 0
    try:
        from backend.signals.engine import detect_regime
        regime_state = detect_regime()
    except Exception:
        regime_state = None

    # ── KPI row ────────────────────────────────────────────────────────────
    c1, c2, c3, c4, c5 = st.columns(5)
    metric(c1, "Portfolio Value", f"€{equity_eur:,.2f}",
           delta=f"{cum_pnl_pct:+.2f}% all-time")
    metric(c2, "Cash Available", f"€{cash_eur:,.2f}",
           delta=f"{cash_usd/equity_usd*100:.0f}% of portfolio" if equity_usd else None)
    metric(c3, "Total Trades", trade_stats.get("total", 0))
    metric(c4, "Win Rate",
           f"{trade_stats.get('win_rate', 0):.1f}%",
           delta=f"W:{trade_stats.get('wins',0)} L:{trade_stats.get('losses',0)}")
    metric(c5, "Total P&L",
           f"€{trade_stats.get('total_pnl_eur', 0):+.2f}",
           delta=f"avg {trade_stats.get('avg_pnl',0):+.3f}%/trade")

    if ceiling and alpaca_actual:
        st.info(
            f"Simulating €{ceiling:,.0f} of the Alpaca paper account. "
            f"Actual Alpaca balance: ${alpaca_actual:,.0f} "
            f"(not used — ceiling applied at €{ceiling:,.0f} ≈ ${ceiling * fx_rate:,.0f})"
        )

    # Count open momentum swing positions
    try:
        from database.client import get_open_trade_records
        open_records  = get_open_trade_records()
        open_swings   = [r for r in open_records if r.get("promoted_to_swing")]
        max_swings    = 2  # profile default; could read from env
    except Exception:
        open_swings   = []
        max_swings    = 2

    if regime_state:
        label = regime_state.market_regime.upper()
        icon = "🐂" if label == "BULL" else ("🐻" if label == "BEAR" else "↔")
        swing_frag = ""
        if open_swings:
            tickers_str = ", ".join(r["ticker"] for r in open_swings[:3])
            swing_frag = (f'<span style="margin-left:20px;color:#ffd166">'
                          f'🚀 Open swings: {len(open_swings)}/{max_swings} ({tickers_str})'
                          f'</span>')
        else:
            swing_frag = (f'<span style="margin-left:20px;color:#555">'
                          f'🚀 Open swings: 0/{max_swings}'
                          f'</span>')
        st.markdown(f"""
        <div style="margin:12px 0 4px;padding:12px 14px;border:1px solid #222;border-radius:8px;background:#101010">
          <span style="font-size:22px;font-weight:700">{icon} {label}</span>
          <span style="margin-left:16px;color:#aaa">VIX {regime_state.vix:.1f}</span>
          <span style="margin-left:16px;color:#777">SPY vs SMA200 {regime_state.price_vs_sma200_pct:+.2f}%</span>
          {swing_frag}
        </div>
        """, unsafe_allow_html=True)

    # ── Open swing position cards ──────────────────────────────────────────
    if open_swings:
        section_title("Open Momentum Swings")
        cols = st.columns(min(len(open_swings), 3))
        for i, rec in enumerate(open_swings):
            ticker     = rec.get("ticker", "?")
            entry      = float(rec.get("entry_price") or 0)
            conviction = float(rec.get("swing_conviction") or 0)
            days_held  = 0
            if rec.get("promoted_at"):
                try:
                    from datetime import datetime
                    promoted_dt = datetime.fromisoformat(
                        str(rec["promoted_at"]).replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                    days_held = (datetime.utcnow() - promoted_dt).days
                except Exception:
                    pass
            max_days = int(rec.get("max_hold_minutes", 1950)) // 390
            days_rem  = max(0, max_days - days_held)
            # Get current price from positions list
            pos_match = next((p for p in positions if p["ticker"] == ticker), None)
            pnl_str   = f"{pos_match['unrealized_plpc']:+.2f}%" if pos_match else "—"
            pnl_color = "positive" if pos_match and pos_match["unrealized_pl"] >= 0 else "negative"
            with cols[i % 3]:
                st.markdown(f"""
                <div class="signal-card">
                    <div class="signal-name">🚀 {ticker}</div>
                    <div style="font-family:'DM Mono',monospace;font-size:13px">
                        Entry ${entry:.2f} · Conv {conviction:.0%}
                    </div>
                    <div class="signal-score {pnl_color}" style="font-size:18px">{pnl_str}</div>
                    <div style="font-size:11px;color:#555">{days_held}d held · {days_rem}d remaining</div>
                </div>""", unsafe_allow_html=True)

    st.markdown("---")

    # ── Equity curve ───────────────────────────────────────────────────────
    col_chart, col_pos = st.columns([2, 1])

    with col_chart:
        section_title("Equity Curve")
        if snapshots:
            df = pd.DataFrame(snapshots)
            df["snapshot_at"] = pd.to_datetime(df["snapshot_at"])
            df = df.sort_values("snapshot_at")
            df["total_value_display_eur"] = pd.to_numeric(
                df["total_value_eur"], errors="coerce"
            )

            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=df["snapshot_at"],
                y=df["total_value_display_eur"],
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
        section_title("Open Positions")
        if positions:
            for p in positions:
                pnl_color = "positive" if p["unrealized_pl"] >= 0 else "negative"
                st.markdown(f"""
                <div class="signal-card">
                    <div class="signal-name">{p['ticker']}</div>
                    <div style="font-family:'DM Mono',monospace;font-size:14px">
                        {p['qty']:.4f} @ ${p['avg_entry']:.2f}
                    </div>
                    <div class="signal-score {pnl_color}" style="font-size:18px">
                        ${p['unrealized_pl']:+.2f} ({p['unrealized_plpc']:+.2f}%)
                    </div>
                </div>""", unsafe_allow_html=True)
        else:
            st.info("No open positions")

    # ── Daily P&L bar chart ────────────────────────────────────────────────
    if snapshots and len(snapshots) > 1:
        section_title("Daily P&L")
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
        section_title("Performance by Market Regime")
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
    metric(c1, "Portfolio Value", "€100.00", delta="paper mode")
    metric(c2, "Cash Available", "€100.00", delta="100%")
    metric(c3, "Total Trades", "0")
    metric(c4, "Win Rate", "—")
    metric(c5, "Total P&L", "€0.00")
