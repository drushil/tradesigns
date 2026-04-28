"""frontend/pages/signals.py — Live signal monitor."""
import streamlit as st
import plotly.graph_objects as go
import pandas as pd
from datetime import datetime
import os


def render():
    st.title("📡 Live Signals")
    st.caption("Real-time signal scores per ticker · Updates every 5 minutes during market hours")

    tickers = [t.strip() for t in os.getenv("TICKER_UNIVERSE", "SPY,QQQ,GLD").split(",")]
    profile_name = os.getenv("RISK_PROFILE", "moderate")

    # Manual refresh
    col_r, col_info = st.columns([1, 4])
    with col_r:
        run_now = st.button("🔄 Compute Signals Now", use_container_width=True)
    with col_info:
        st.info("Signals are computed automatically every 5 min. Click above to refresh manually.")

    if run_now:
        _compute_and_display_live(tickers, profile_name)
    else:
        _display_from_db(tickers)


def _compute_and_display_live(tickers, profile_name):
    """Compute fresh signals for all tickers and display."""
    from backend.signals.engine import compute_all_signals, detect_regime
    from backend.learning.engine import RegimeAwareWeightEngine
    from database.client import get_latest_weights, insert_signal
    from config.risk_profiles import get_profile

    profile = get_profile(profile_name)
    saved_weights = get_latest_weights("global")
    weights = saved_weights if saved_weights else profile["signal_weights"]

    regime = detect_regime()
    st.markdown(f"**Current regime:** `{regime}`")

    progress = st.progress(0)
    all_results = {}

    for i, ticker in enumerate(tickers):
        with st.spinner(f"Computing {ticker}..."):
            try:
                result = compute_all_signals(ticker, weights)
                all_results[ticker] = result
                insert_signal({
                    "ticker":                ticker,
                    "composite_score":       result["composite_score"],
                    "order_book_score":      result["signals"].get("order_book_imbalance", {}).get("score", 0),
                    "tape_aggression_score": result["signals"].get("tape_aggression", {}).get("score", 0),
                    "rsi_divergence_score":  result["signals"].get("rsi_divergence", {}).get("score", 0),
                    "news_sentiment_score":  result["signals"].get("news_sentiment", {}).get("score", 0),
                    "vwap_deviation_score":  result["signals"].get("vwap_deviation", {}).get("score", 0),
                    "regime":                regime,
                    "gated":                 False,
                    "llm_called":            False,
                })
            except Exception as e:
                all_results[ticker] = {"error": str(e)}
        progress.progress((i + 1) / len(tickers))

    _render_signal_cards(all_results, weights)


def _display_from_db(tickers):
    """Show most recent signals from DB."""
    try:
        from database.client import get_recent_signals
        signals = get_recent_signals(hours=2)
        if not signals:
            st.info("No signals in database yet. Click 'Compute Signals Now' to start.")
            return
        # Group by ticker, take latest
        latest = {}
        for s in signals:
            t = s.get("ticker")
            if t not in latest:
                latest[t] = s

        _render_signal_cards_from_db(latest)
    except Exception as e:
        st.error(f"DB error: {e}")


def _render_signal_cards(results: dict, weights: dict):
    st.markdown("---")
    SIGNAL_LABELS = {
        "order_book_imbalance": "Order Book Imbalance",
        "tape_aggression":      "Tape Aggression",
        "rsi_divergence":       "RSI Divergence",
        "news_sentiment":       "News Sentiment",
        "vwap_deviation":       "VWAP Deviation",
    }

    for ticker, result in results.items():
        if "error" in result:
            st.error(f"{ticker}: {result['error']}")
            continue

        composite = result["composite_score"]
        c_class   = "positive" if composite > 0.1 else ("negative" if composite < -0.1 else "neutral")
        action    = "🟢 BULLISH" if composite > 0.35 else ("🔴 BEARISH" if composite < -0.35 else "⚪ NEUTRAL")

        with st.expander(f"**{ticker}** — Composite: `{composite:+.3f}` — {action}", expanded=True):
            col_comp, col_breakdown = st.columns([1, 2])

            with col_comp:
                # Gauge
                fig = go.Figure(go.Indicator(
                    mode="gauge+number",
                    value=composite,
                    domain={"x": [0,1], "y": [0,1]},
                    number={"suffix": "", "font": {"size": 28, "color": "#fff", "family": "DM Mono"}},
                    gauge={
                        "axis": {"range": [-1, 1], "tickcolor": "#444",
                                 "tickvals": [-1, -0.5, 0, 0.5, 1]},
                        "bar": {"color": "#00d4a0" if composite >= 0 else "#ff5c5c"},
                        "bgcolor": "#111",
                        "bordercolor": "#222",
                        "steps": [
                            {"range": [-1, -0.35], "color": "rgba(255,92,92,0.15)"},
                            {"range": [-0.35, 0.35], "color": "rgba(255,255,255,0.03)"},
                            {"range": [0.35, 1],  "color": "rgba(0,212,160,0.15)"},
                        ],
                    }
                ))
                fig.update_layout(
                    paper_bgcolor="rgba(0,0,0,0)",
                    font_color="#888",
                    height=200,
                    margin=dict(l=10, r=10, t=20, b=10),
                )
                st.plotly_chart(fig, use_container_width=True)

            with col_breakdown:
                st.markdown("**Individual signal scores**")
                for sig_key, sig_data in result["signals"].items():
                    score = sig_data.get("score", 0)
                    w     = weights.get(sig_key, 0.2)
                    label = SIGNAL_LABELS.get(sig_key, sig_key)
                    bar_w = int(abs(score) * 100)
                    color = "#00d4a0" if score > 0 else ("#ff5c5c" if score < 0 else "#444")

                    st.markdown(f"""
                    <div style="margin-bottom:8px">
                      <div style="display:flex;justify-content:space-between;font-size:12px;color:#888;margin-bottom:3px">
                        <span>{label}</span>
                        <span style="font-family:'DM Mono',monospace">{score:+.3f} · weight {w:.0%}</span>
                      </div>
                      <div style="background:#1a1a1a;border-radius:3px;height:6px">
                        <div style="width:{bar_w}%;height:100%;background:{color};border-radius:3px"></div>
                      </div>
                    </div>""", unsafe_allow_html=True)

                # Meta info
                news_meta = result["signals"].get("news_sentiment", {}).get("meta", {})
                if news_meta.get("latest_headline"):
                    st.caption(f"📰 Latest: {news_meta['latest_headline']}")


def _render_signal_cards_from_db(latest: dict):
    for ticker, row in latest.items():
        composite = row.get("composite_score", 0) or 0
        action = "🟢 BULLISH" if composite > 0.35 else ("🔴 BEARISH" if composite < -0.35 else "⚪ NEUTRAL")
        gated  = row.get("gated", False)

        with st.expander(f"**{ticker}** — `{composite:+.3f}` — {action} {'🚫 GATED' if gated else ''}", expanded=False):
            cols = st.columns(5)
            signal_map = {
                "Order Book":    row.get("order_book_score", 0),
                "Tape Aggrssn": row.get("tape_aggression_score", 0),
                "RSI Diverg":   row.get("rsi_divergence_score", 0),
                "News Sntmnt":  row.get("news_sentiment_score", 0),
                "VWAP Dev":     row.get("vwap_deviation_score", 0),
            }
            for i, (name, score) in enumerate(signal_map.items()):
                score = score or 0
                color = "normal" if abs(score) < 0.1 else ("normal" if score > 0 else "inverse")
                cols[i].metric(name, f"{score:+.3f}")

            if gated:
                st.warning(f"Gated: {row.get('gate_reason', 'unknown reason')}")
            ts = row.get("created_at", "")[:19]
            st.caption(f"Computed at: {ts} UTC · Regime: {row.get('regime', '—')}")
