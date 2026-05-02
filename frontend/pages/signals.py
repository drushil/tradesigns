"""frontend/pages/signals.py — Live signal monitor (10-signal engine)."""
import streamlit as st
import plotly.graph_objects as go
import pandas as pd
import os
from frontend.ticker_profiles import ticker_profile_html

# ── All 10 signals + earnings multiplier ─────────────────────────────────────
ALL_SIGNALS = {
    "order_book_imbalance": {
        "label": "Order Book",
        "desc":  "Bid/ask pressure via Alpaca quote",
        "db_col": "order_book_score",
        "new": False,
    },
    "tape_aggression": {
        "label": "Tape Aggression",
        "desc":  "Volume spike × price momentum",
        "db_col": "tape_aggression_score",
        "new": False,
    },
    "rsi_divergence": {
        "label": "RSI Divergence",
        "desc":  "Overbought/oversold + price-RSI divergence",
        "db_col": "rsi_divergence_score",
        "new": False,
    },
    "news_sentiment": {
        "label": "News Sentiment",
        "desc":  "Keyword NLP on recent headlines",
        "db_col": "news_sentiment_score",
        "new": False,
    },
    "vwap_deviation": {
        "label": "VWAP Deviation",
        "desc":  "Distance from intraday VWAP",
        "db_col": "vwap_deviation_score",
        "new": False,
    },
    "macd_crossover": {
        "label": "MACD Crossover",
        "desc":  "Momentum direction on 5-min bars",
        "db_col": "macd_score",
        "new": False,
    },
    "relative_strength": {
        "label": "Relative Strength",
        "desc":  "Performance vs SPY over 5/10/20 bars",
        "db_col": "rel_strength_score",
        "new": False,
    },
    "bollinger_squeeze": {
        "label": "BB Squeeze",
        "desc":  "Bollinger Band squeeze breakout on 5-min bars",
        "db_col": "bollinger_score",
        "new": True,
    },
    "put_call_ratio": {
        "label": "Put/Call Ratio",
        "desc":  "Options sentiment from nearest expiry",
        "db_col": "put_call_score",
        "new": True,
    },
    "mean_reversion": {
        "label": "Mean Reversion",
        "desc":  "Bull-regime ETF oversold swing setup",
        "db_col": None,
        "new": True,
    },
    "earnings_proximity": {
        "label": "Earnings Proximity",
        "desc":  "Days to next earnings (multiplier signal)",
        "db_col": None,   # stored as earnings_days / earnings_mult, not a score column
        "new": False,
    },
}


def render():
    st.title("📡 Live Signals")
    st.caption("10-signal engine · Updates every 5 minutes during market hours")

    tickers = [t.strip() for t in os.getenv("TICKER_UNIVERSE", "SPY,QQQ,GLD").split(",")]
    profile_name = os.getenv("RISK_PROFILE", "moderate")

    weighted_count = sum(1 for meta in ALL_SIGNALS.values() if meta["db_col"])
    c_total, c_weighted, c_multiplier = st.columns(3)
    c_total.metric("Signals", len(ALL_SIGNALS))
    c_weighted.metric("Weighted scores", weighted_count)
    c_multiplier.metric("Multiplier", "Earnings")

    col_r, col_info = st.columns([1, 4])
    with col_r:
        run_now = st.button("🔄 Compute Signals Now", width="stretch")
    with col_info:
        st.info("Auto-refreshes every 5 min during market hours. Click to compute now.")

    # ── Signal legend ──────────────────────────────────────────────────────────
    with st.expander("ℹ️ About these 10 signals", expanded=False):
        cols = st.columns(2)
        for i, (key, meta) in enumerate(ALL_SIGNALS.items()):
            with cols[i % 2]:
                badge = " 🆕" if meta["new"] else ""
                st.markdown(f"""
                <div style="padding:8px 0;border-bottom:0.5px solid #1a1a1a;margin-bottom:6px">
                  <span style="font-size:13px;font-weight:500;color:#eee">{meta['label']}{badge}</span><br>
                  <span style="font-size:11px;color:#666">{meta['desc']}</span>
                </div>""", unsafe_allow_html=True)
        st.markdown("""
        <div style="font-size:11px;color:#666;margin-top:8px;line-height:1.6">
        <b style="color:#888">Earnings proximity</b> acts as a <em>multiplier</em> (×1.0–1.5) on the composite
        score rather than a weighted component — it amplifies signals in the 48h before earnings.
        </div>""", unsafe_allow_html=True)

    if run_now:
        _compute_and_display_live(tickers, profile_name)
    else:
        _display_from_db(tickers)


# ── Live compute path ─────────────────────────────────────────────────────────

def _compute_and_display_live(tickers, profile_name):
    from backend.signals.engine import compute_all_signals, detect_regime, detect_macro_regime
    from backend.broker.alpaca import compute_position_size
    from database.client import get_latest_weights, insert_signal
    from config.risk_profiles import get_profile

    profile       = get_profile(profile_name)
    saved_weights = get_latest_weights("global")
    weights       = saved_weights if saved_weights else profile["signal_weights"]
    regime_state  = detect_regime()
    regime        = regime_state.intraday_regime
    macro_regime  = detect_macro_regime()

    st.markdown(
        f"**Regime:** `{regime_state.market_regime.upper()}` · "
        f"**Intraday:** `{regime}` · **VIX:** `{regime_state.vix:.1f}` · "
        f"**Macro:** `{macro_regime}`"
    )
    progress    = st.progress(0)
    all_results = {}
    shock_banner = None

    for i, ticker in enumerate(tickers):
        with st.spinner(f"Computing {ticker}..."):
            try:
                result = compute_all_signals(ticker, weights, regime_state=regime_state)
                atr_data = result.get("atr_data", {})
                sizing = compute_position_size(
                    ticker,
                    float(os.getenv("STARTING_CAPITAL_EUR", "100")),
                    profile,
                    0.7,
                    atr_data,
                    regime_state,
                )
                result["sizing_preview"] = sizing
                if result.get("shock_detected") and not shock_banner:
                    shock_banner = result.get("shock_result", {})
                all_results[ticker] = result
                sigs          = result["signals"]
                earnings_meta = sigs.get("earnings_proximity", {}).get("meta", {})
                insert_signal({
                    "ticker":                ticker,
                    "composite_score":       result["composite_score"],
                    "order_book_score":      sigs.get("order_book_imbalance", {}).get("score", 0),
                    "tape_aggression_score": sigs.get("tape_aggression",      {}).get("score", 0),
                    "rsi_divergence_score":  sigs.get("rsi_divergence",       {}).get("score", 0),
                    "news_sentiment_score":  sigs.get("news_sentiment",       {}).get("score", 0),
                    "vwap_deviation_score":  sigs.get("vwap_deviation",       {}).get("score", 0),
                    "macd_score":            sigs.get("macd_crossover",       {}).get("score", 0),
                    "rel_strength_score":    sigs.get("relative_strength",    {}).get("score", 0),
                    "bollinger_score":       sigs.get("bollinger_squeeze",    {}).get("score", 0),
                    "put_call_score":        sigs.get("put_call_ratio",       {}).get("score", 0),
                    "atr_pct":               atr_data.get("atr_pct"),
                    "earnings_days":         earnings_meta.get("days_to_earnings"),
                    "earnings_mult":         earnings_meta.get("earnings_multiplier", 1.0),
                    "macro_regime":          result.get("macro_regime"),
                    "macro_multiplier":      result.get("macro_multiplier", 1.0),
                    "market_regime":         result.get("market_regime") or result.get("regime_bull_bear"),
                    "shock_detected":        result.get("shock_detected", False),
                    "shock_classification":  result.get("shock_classification"),
                    "regime":                regime,
                    "gated":                 False,
                    "llm_called":            False,
                })
            except Exception as e:
                all_results[ticker] = {"error": str(e)}
        progress.progress((i + 1) / len(tickers))

    if shock_banner:
        st.error(
            f"Macro shock: {shock_banner.get('reason', 'No reason supplied')} "
            f"| affected: {', '.join(shock_banner.get('affected_sectors') or [])}"
        )

    _render_live_cards(all_results, weights)


# ── DB display path ───────────────────────────────────────────────────────────

# ── DB display path ───────────────────────────────────────────────────────────

def _display_from_db(tickers):
    try:
        from database.client import get_recent_signals
        signals = get_recent_signals(hours=2)
        if not signals:
            st.info("No signals yet. Click 'Compute Signals Now' to start.")
            return
        latest = {}
        for s in signals:
            t = s.get("ticker")
            if t not in latest:
                latest[t] = s
        shock_rows = [s for s in latest.values() if s.get("shock_detected")]
        if shock_rows:
            row = shock_rows[0]
            st.error(
                f"Macro shock active: {row.get('shock_classification') or 'SHOCK'}"
            )
        missing_cols = _missing_new_signal_columns(latest.values())
        if missing_cols:
            st.warning(
                "Supabase is still returning the original 5-signal schema. "
                "Apply the `database/schema.sql` upgrade so MACD, Relative Strength, "
                "and Earnings Proximity can be stored. Live compute still uses all 8."
            )
        _render_db_cards(latest)
    except Exception as e:
        st.error(f"DB error: {e}")


def _missing_new_signal_columns(rows) -> list:
    required = [
        "macd_score", "rel_strength_score", "earnings_days", "earnings_mult",
        "bollinger_score", "put_call_score", "atr_pct",
        "macro_regime", "macro_multiplier", "regime_bull_bear",
        "shock_detected", "shock_classification",
    ]
    for row in rows:
        return [col for col in required if col not in row]
    return []


# ── Live render (full 8-signal breakdown) ─────────────────────────────────────

def _render_live_cards(results: dict, weights: dict):
    st.markdown("---")

    for ticker, result in results.items():
        if "error" in result:
            st.error(f"{ticker}: {result['error']}")
            continue

        composite = result["composite_score"]
        action    = "🟢 BULLISH" if composite > 0.35 else ("🔴 BEARISH" if composite < -0.35 else "⚪ NEUTRAL")
        e_mult    = result.get("earnings_multiplier", 1.0)
        macro_regime = result.get("macro_regime", "normal")
        macro_mult = result.get("macro_multiplier", 1.0)
        regime_state = result.get("regime_state", {})
        market_regime = str(regime_state.get("market_regime", result.get("regime_bull_bear", "transitioning"))).upper()
        shock = result.get("shock_detected", False)
        regime_badge = "⚡ SHOCK" if shock else ("🐂 BULL" if market_regime == "BULL" else ("🐻 BEAR" if market_regime == "BEAR" else "TRANSITIONING"))
        e_days    = (result["signals"].get("earnings_proximity", {})
                     .get("meta", {}).get("days_to_earnings"))
        mr_meta   = result["signals"].get("mean_reversion", {}).get("meta", {})
        mr_active = mr_meta.get("mean_reversion_signal", False)

        with st.expander(
            f"**{ticker}** — `{composite:+.3f}` — {action} — {regime_badge} — macro `{macro_regime}` ×{macro_mult:.2f}"
            + (f" 📅 Earnings in {e_days}d" if e_days is not None and e_days <= 5 else "")
            + (" 🔄 MR" if mr_active else ""),
            expanded=True
        ):
            profile_html = ticker_profile_html(ticker, compact=True)
            if profile_html:
                st.markdown(profile_html, unsafe_allow_html=True)

            col_gauge, col_signals = st.columns([1, 2])

            # Gauge
            with col_gauge:
                fig = go.Figure(go.Indicator(
                    mode="gauge+number",
                    value=composite,
                    domain={"x": [0,1], "y": [0,1]},
                    number={"font": {"size": 26, "color": "#fff", "family": "DM Mono"}},
                    gauge={
                        "axis": {"range": [-1, 1], "tickcolor": "#444",
                                 "tickvals": [-1, -0.5, 0, 0.5, 1]},
                        "bar": {"color": "#00d4a0" if composite >= 0 else "#ff5c5c"},
                        "bgcolor": "#111", "bordercolor": "#222",
                        "steps": [
                            {"range": [-1, -0.35], "color": "rgba(255,92,92,0.12)"},
                            {"range": [-0.35, 0.35], "color": "rgba(255,255,255,0.02)"},
                            {"range": [0.35, 1],   "color": "rgba(0,212,160,0.12)"},
                        ],
                    }
                ))
                fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", font_color="#888",
                                  height=190, margin=dict(l=8, r=8, t=16, b=8))
                st.plotly_chart(fig, width="stretch")

                if e_mult > 1.0:
                    st.markdown(
                        f"<div style='text-align:center;font-size:11px;color:#EF9F27'>"
                        f"Earnings multiplier ×{e_mult:.1f}</div>",
                        unsafe_allow_html=True
                    )
                atr_data = result.get("atr_data", {})
                sizing = result.get("sizing_preview", {})
                st.metric("ATR", f"{atr_data.get('atr_pct', 0) or 0:.3f}%")
                if sizing:
                    st.metric("Size preview", f"EUR {sizing.get('size_eur', 0):,.2f}",
                              delta=f"stop {sizing.get('stop_pct', 0):.2f}%")

            # 8-signal breakdown
            with col_signals:
                sigs = result["signals"]
                for sig_key, meta in ALL_SIGNALS.items():
                    if sig_key == "earnings_proximity":
                        # Show earnings as a special row
                        ed   = (sigs.get("earnings_proximity", {})
                                .get("meta", {}).get("days_to_earnings"))
                        mult = (sigs.get("earnings_proximity", {})
                                .get("meta", {}).get("earnings_multiplier", 1.0))
                        color = "#EF9F27" if mult > 1.0 else "#444"
                        label_txt = (f"{ed}d to earnings · ×{mult:.1f}"
                                     if ed is not None else "earnings: unknown")
                        st.markdown(f"""
                        <div style="margin-bottom:8px">
                          <div style="display:flex;justify-content:space-between;
                               font-size:12px;color:#888;margin-bottom:3px">
                            <span>{meta['label']} 🆕 <span style="font-size:10px">(multiplier)</span></span>
                            <span style="font-family:'DM Mono',monospace;color:{color}">{label_txt}</span>
                          </div>
                          <div style="background:#1a1a1a;border-radius:3px;height:4px">
                            <div style="width:{min((mult-1)*100,100):.0f}%;height:100%;
                                 background:{color};border-radius:3px"></div>
                          </div>
                        </div>""", unsafe_allow_html=True)
                        continue

                    sig_data = sigs.get(sig_key, {})
                    score    = sig_data.get("score", 0) or 0
                    w        = weights.get(sig_key, 0.0)
                    bar_w    = int(abs(score) * 100)
                    color    = "#00d4a0" if score > 0 else ("#ff5c5c" if score < 0 else "#444")
                    new_badge= " 🆕" if meta["new"] else ""

                    st.markdown(f"""
                    <div style="margin-bottom:8px">
                      <div style="display:flex;justify-content:space-between;
                           font-size:12px;color:#888;margin-bottom:3px">
                        <span>{meta['label']}{new_badge}</span>
                        <span style="font-family:'DM Mono',monospace">
                          <span style="color:{color}">{score:+.3f}</span>
                          &nbsp;·&nbsp;wt {w:.0%}</span>
                      </div>
                      <div style="background:#1a1a1a;border-radius:3px;height:5px">
                        <div style="width:{bar_w}%;height:100%;background:{color};
                             border-radius:3px"></div>
                      </div>
                    </div>""", unsafe_allow_html=True)

                # Show news headline if available
                news_meta = sigs.get("news_sentiment", {}).get("meta", {})
                if news_meta.get("latest_headline"):
                    src = news_meta.get("source", "")
                    st.caption(f"📰 [{src}] {news_meta['latest_headline']}")

                # Show RS context
                rs_meta = sigs.get("relative_strength", {}).get("meta", {})
                if rs_meta.get("rs_10bar") is not None:
                    rs = rs_meta["rs_10bar"]
                    spy_ret = rs_meta.get("spy_ret_10bar", 0)
                    tick_ret = rs_meta.get("ticker_ret_10bar", 0)
                    color = "#00d4a0" if rs > 0 else "#ff5c5c"
                    st.caption(
                        f"📊 vs SPY (10 bars): {tick_ret:+.2f}% vs {spy_ret:+.2f}% "
                        f"→ RS <span style='color:{color}'>{rs:+.2f}%</span>",
                        unsafe_allow_html=True
                    )

                # MACD context
                macd_meta = sigs.get("macd_crossover", {}).get("meta", {})
                if macd_meta.get("crossed_up"):
                    st.caption("⚡ MACD bullish crossover detected")
                elif macd_meta.get("crossed_down"):
                    st.caption("⚡ MACD bearish crossover detected")

                # Bollinger squeeze context
                bb_meta = sigs.get("bollinger_squeeze", {}).get("meta", {})
                if bb_meta.get("squeeze"):
                    bw_ratio = bb_meta.get("bw_ratio", 0)
                    st.caption(f"🔵 BB squeeze active · band width {bw_ratio:.2f}× avg")

                # Put/call context
                pcr_meta = sigs.get("put_call_ratio", {}).get("meta", {})
                pcr_val  = pcr_meta.get("pcr")
                if pcr_val is not None:
                    st.caption(
                        f"📉 PCR {pcr_val:.2f} "
                        f"(puts {pcr_meta.get('put_volume',0):,} / calls {pcr_meta.get('call_volume',0):,})"
                    )

                # Mean reversion context
                if mr_active:
                    st.caption(
                        f"🔄 Mean reversion: RSI2={mr_meta.get('rsi_2', '?')}, "
                        f"{mr_meta.get('consecutive_down_days', '?')} down days · "
                        f"Hold target: {mr_meta.get('hold_days', 2)} days"
                    )


# ── DB render (compact — 8 metrics in 2 rows) ─────────────────────────────────

def _render_db_cards(latest: dict):
    def fmt_score(row: dict, col: str) -> str:
        if col not in row:
            return "not stored"
        return f"{row.get(col, 0) or 0:+.3f}"

    for ticker, row in latest.items():
        composite = row.get("composite_score", 0) or 0
        action    = "🟢 BULLISH" if composite > 0.35 else ("🔴 BEARISH" if composite < -0.35 else "⚪ NEUTRAL")
        gated     = row.get("gated", False)
        e_days    = row.get("earnings_days")
        e_mult    = row.get("earnings_mult", 1.0) or 1.0
        macro_regime = row.get("macro_regime") or "normal"
        macro_mult = row.get("macro_multiplier", 1.0) or 1.0
        market_regime = str(row.get("market_regime") or row.get("regime_bull_bear") or "transitioning").upper()
        shock = bool(row.get("shock_detected"))
        mr_active = bool(row.get("mean_rev_signal", False))
        regime_badge = "⚡ SHOCK" if shock else ("🐂 BULL" if market_regime == "BULL" else ("🐻 BEAR" if market_regime == "BEAR" else "TRANSITIONING"))

        if shock:
            st.error(f"Macro shock active: {row.get('shock_classification') or 'SHOCK'}")

        header = (f"**{ticker}** — `{composite:+.3f}` — {action}"
                  + f" — {regime_badge} — macro `{macro_regime}` ×{macro_mult:.2f}"
                  + (" 🚫 GATED" if gated else "")
                  + (f" 📅 Earnings in {e_days}d" if e_days is not None and e_days <= 5 else "")
                  + (" 🔄 MR" if mr_active else ""))

        with st.expander(header, expanded=False):
            profile_html = ticker_profile_html(ticker, compact=True)
            if profile_html:
                st.markdown(profile_html, unsafe_allow_html=True)

            # Row 1: original 5
            st.markdown("**Original signals**")
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Order Book",   f"{row.get('order_book_score',    0) or 0:+.3f}")
            c2.metric("Tape Aggrssn",f"{row.get('tape_aggression_score',0) or 0:+.3f}")
            c3.metric("RSI Diverg",  f"{row.get('rsi_divergence_score', 0) or 0:+.3f}")
            c4.metric("News Sntmnt", f"{row.get('news_sentiment_score', 0) or 0:+.3f}")
            c5.metric("VWAP Dev",    f"{row.get('vwap_deviation_score', 0) or 0:+.3f}")

            # Row 2: MACD, RS, Bollinger, Put/Call
            st.markdown("**New signals 🆕**")
            n1, n2, n3, n4 = st.columns(4)
            n1.metric("MACD",         fmt_score(row, "macd_score"))
            n2.metric("Rel Strength", fmt_score(row, "rel_strength_score"))
            n3.metric("BB Squeeze",   fmt_score(row, "bollinger_score"))
            n4.metric("Put/Call",     fmt_score(row, "put_call_score"))

            # Row 3: earnings + ATR
            st.markdown("**Multiplier & volatility**")
            x1, x2, x3 = st.columns(3)
            x1.metric(
                "Earnings ×",
                "not stored" if "earnings_mult" not in row else (
                    f"×{e_mult:.1f}" + (f" ({e_days}d)" if e_days is not None else "")
                ),
            )
            atr_val = row.get("atr_pct")
            x2.metric("ATR %", f"{atr_val:.3f}%" if atr_val else "—")
            x3.metric("Regime", f"{market_regime} | {macro_regime} ×{macro_mult:.2f}")

            if gated:
                st.warning(f"Gated: {row.get('gate_reason', '—')}")
            ts = str(row.get("created_at", ""))[:19]
            st.caption(f"Computed {ts} UTC · Regime: {row.get('regime', '—')}")
