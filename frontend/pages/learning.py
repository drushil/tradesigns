"""frontend/pages/learning.py — Learning engine visualisation."""
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd


SIGNAL_LABELS = {
    "order_book":      "Order Book Imbalance",
    "tape_aggression": "Tape Aggression",
    "rsi_divergence":  "RSI Divergence",
    "news_sentiment":  "News Sentiment",
    "vwap_deviation":  "VWAP Deviation",
    "macd_crossover":  "MACD Crossover",
    "relative_strength": "Relative Strength",
    "bollinger_squeeze": "BB Squeeze",
    "put_call_ratio": "Put/Call Ratio",
}
SIGNAL_COLORS = ["#00d4a0", "#6c63ff", "#ffd166", "#ff5c5c",
                 "#4ecdc4", "#ef9f27", "#9b8cff", "#2dd4bf", "#f472b6"]


def render():
    st.title("🧠 Learning Engine")
    st.caption("How the agent learns from every trade to improve signal weights")

    try:
        from database.client import get_weight_history, get_learnings, get_recent_trades
        weight_history = get_weight_history("global", limit=60)
        learnings      = get_learnings(limit=8)
        trades         = get_recent_trades(days=30)
    except Exception as e:
        st.error(f"DB error: {e}")
        return

    # ── Current weights ────────────────────────────────────────────────────
    st.markdown("### Current Signal Weights")
    st.caption("Weights update after each trade via Exponential Weight Averaging (EWA)")

    if weight_history:
        latest = weight_history[0]
        weights_data = {
            "Order Book Imbalance": latest.get("order_book", 0.30),
            "Tape Aggression":      latest.get("tape_aggression", 0.25),
            "RSI Divergence":       latest.get("rsi_divergence", 0.15),
            "News Sentiment":       latest.get("news_sentiment", 0.20),
            "VWAP Deviation":       latest.get("vwap_deviation", 0.10),
            "MACD Crossover":       latest.get("macd_crossover", 0.10),
            "Relative Strength":    latest.get("relative_strength", 0.08),
            "BB Squeeze":           latest.get("bollinger_squeeze", 0.09),
            "Put/Call Ratio":       latest.get("put_call_ratio", 0.05),
        }
        st.caption(f"Last updated: {latest.get('updated_at','—')[:19]} UTC · "
                   f"Trigger: `{latest.get('trigger','—')}` · "
                   f"After {latest.get('trade_count','?')} trades")
    else:
        # Show priors
        from config.risk_profiles import get_profile
        import os
        profile = get_profile(os.getenv("RISK_PROFILE", "moderate"))
        sw = profile["signal_weights"]
        weights_data = {
            "Order Book Imbalance": sw.get("order_book_imbalance", 0.30),
            "Tape Aggression":      sw.get("tape_aggression", 0.25),
            "RSI Divergence":       sw.get("rsi_divergence", 0.15),
            "News Sentiment":       sw.get("news_sentiment", 0.20),
            "VWAP Deviation":       sw.get("vwap_deviation", 0.10),
            "MACD Crossover":       sw.get("macd_crossover", 0.10),
            "Relative Strength":    sw.get("relative_strength", 0.08),
            "BB Squeeze":           sw.get("bollinger_squeeze", 0.09),
            "Put/Call Ratio":       sw.get("put_call_ratio", 0.05),
        }
        st.info("No weight updates yet — showing profile priors. Start trading to enable learning.")

    # Horizontal bar chart
    fig_w = go.Figure(go.Bar(
        x=list(weights_data.values()),
        y=list(weights_data.keys()),
        orientation="h",
        marker_color=SIGNAL_COLORS,
        text=[f"{v:.1%}" for v in weights_data.values()],
        textposition="outside",
    ))
    fig_w.update_layout(
        template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)", height=220,
        margin=dict(l=0, r=60, t=10, b=0),
        xaxis=dict(tickformat=".0%", range=[0, 0.6], gridcolor="#1a1a1a"),
        yaxis=dict(gridcolor="rgba(0,0,0,0)"),
    )
    st.plotly_chart(fig_w, width="stretch")

    # ── Weight evolution chart ─────────────────────────────────────────────
    if len(weight_history) > 3:
        st.markdown("### Weight Evolution Over Time")
        st.caption("How each signal's influence has shifted as the agent learned")

        df_w = pd.DataFrame(weight_history)
        df_w["updated_at"] = pd.to_datetime(df_w["updated_at"])
        df_w = df_w.sort_values("updated_at")

        fig_evo = go.Figure()
        for i, (col, label) in enumerate(SIGNAL_LABELS.items()):
            if col in df_w.columns:
                fig_evo.add_trace(go.Scatter(
                    x=df_w["updated_at"], y=df_w[col],
                    mode="lines+markers", name=label,
                    line=dict(color=SIGNAL_COLORS[i], width=2),
                    marker=dict(size=4),
                ))
        fig_evo.update_layout(
            template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)", height=280,
            margin=dict(l=0, r=0, t=10, b=0),
            yaxis=dict(tickformat=".0%", gridcolor="#1a1a1a"),
            xaxis=dict(gridcolor="#1a1a1a"),
            legend=dict(orientation="h", y=-0.2),
        )
        st.plotly_chart(fig_evo, width="stretch")

    st.markdown("---")

    # ── Signal performance attribution ─────────────────────────────────────
    st.markdown("### Signal Attribution Analysis")
    st.caption("Which signals actually predicted profitable trades")

    if trades:
        # Compute simple attribution from trade records
        sig_cols = {
            "order_book_score":       "Order Book",
            "tape_aggression_score":  "Tape Aggression",
            "rsi_divergence_score":   "RSI Divergence",
            "news_sentiment_score":   "News Sentiment",
            "vwap_deviation_score":   "VWAP Deviation",
            "macd_score":             "MACD Crossover",
            "rel_strength_score":     "Relative Strength",
            "bollinger_score":         "BB Squeeze",
            "put_call_score":          "Put/Call Ratio",
        }

        from database.client import get_recent_signals
        signals_db = get_recent_signals(hours=720)  # 30 days

        if signals_db:
            df_s = pd.DataFrame(signals_db)
            st.markdown("##### Recent Signal Score Distribution")
            for sig_col, sig_label in sig_cols.items():
                if sig_col in df_s.columns:
                    vals = pd.to_numeric(df_s[sig_col], errors="coerce").dropna()
                    if len(vals) > 0:
                        mean_score = vals.mean()
                        color = "#00d4a0" if mean_score > 0 else "#ff5c5c"
                        st.markdown(f"""
                        <div style="margin-bottom:8px;padding:10px 14px;background:#111;
                             border-radius:8px;border:0.5px solid #222">
                          <div style="display:flex;justify-content:space-between;
                               align-items:center">
                            <span style="font-size:13px;color:#ccc">{sig_label}</span>
                            <span style="font-family:'DM Mono',monospace;font-size:14px;
                                  color:{color}">{mean_score:+.3f} avg</span>
                          </div>
                          <div style="margin-top:6px;background:#1a1a1a;
                               border-radius:3px;height:4px">
                            <div style="width:{min(abs(mean_score)*100,100):.0f}%;
                                 height:100%;background:{color};border-radius:3px"></div>
                          </div>
                        </div>""", unsafe_allow_html=True)

    st.markdown("---")

    # ── Weekly insights ────────────────────────────────────────────────────
    st.markdown("### Weekly AI Insights")
    st.caption("Generated by Claude Sonnet every Sunday — patterns discovered from trade history")

    col_gen, _ = st.columns([1, 3])
    with col_gen:
        if st.button("🧠 Generate Insights Now", width="stretch"):
            if trades and len(trades) >= 5:
                with st.spinner("Claude Sonnet is analysing your trade history..."):
                    from backend.learning.engine import generate_weekly_insights
                    from database.client import save_learning
                    from datetime import date
                    insights = generate_weekly_insights(trades)
                    save_learning(date.today(), insights, len(trades))
                    st.success(f"Generated {len(insights)} insights!")
                    learnings = [{"insights_json": insights,
                                  "created_at": str(date.today()),
                                  "trades_analysed": len(trades)}]
            else:
                st.warning("Need at least 5 trades to generate insights.")

    if learnings:
        for learning in learnings:
            insights = learning.get("insights_json", [])
            ts       = str(learning.get("created_at", ""))[:10]
            n_trades = learning.get("trades_analysed", 0)

            st.markdown(f"**Week of {ts}** · {n_trades} trades analysed")

            if isinstance(insights, list):
                for ins in insights:
                    if not isinstance(ins, dict):
                        continue
                    confidence = ins.get("confidence", 0)
                    category   = ins.get("category", "general")
                    cat_color  = {
                        "signals": "#00d4a0", "timing": "#6c63ff",
                        "risk": "#ff5c5c", "costs": "#ffd166",
                        "regime": "#4ecdc4"
                    }.get(category, "#888")

                    st.markdown(f"""
                    <div style="background:#111;border:0.5px solid #222;border-left:3px solid {cat_color};
                         border-radius:8px;padding:14px 16px;margin-bottom:10px">
                      <div style="display:flex;justify-content:space-between;margin-bottom:6px">
                        <span style="font-size:11px;color:{cat_color};text-transform:uppercase;
                              letter-spacing:.06em">{category}</span>
                        <span style="font-size:11px;color:#555">
                          confidence: {confidence:.0%}</span>
                      </div>
                      <div style="font-size:14px;color:#eee;margin-bottom:8px">
                        {ins.get('insight','')}</div>
                      <div style="font-size:12px;color:#888">
                        → {ins.get('action','')}</div>
                    </div>""", unsafe_allow_html=True)
            st.markdown("---")
    else:
        st.info("No weekly insights yet. Generate your first one above, or wait for Sunday's automated run.")

    # ── EV gate stats ──────────────────────────────────────────────────────
    st.markdown("### Expected Value Gate — Cost Control")
    st.caption("Trades blocked because net EV (after fees) was negative")

    try:
        from database.client import get_recent_signals
        sigs = get_recent_signals(hours=168)  # 7 days
        if sigs:
            df_sigs = pd.DataFrame(sigs)
            gated_pct = df_sigs["gated"].mean() * 100 if "gated" in df_sigs.columns else 0
            total     = len(df_sigs)
            gated_n   = int(df_sigs["gated"].sum()) if "gated" in df_sigs.columns else 0

            c1, c2, c3 = st.columns(3)
            c1.metric("Signals computed (7d)", total)
            c2.metric("Signals gated", gated_n, delta=f"{gated_pct:.0f}% of total")
            c3.metric("LLM calls saved", gated_n,
                      delta=f"≈€{gated_n*0.001:.2f} saved")
    except Exception:
        pass

    st.markdown("---")

    # ── Blocked opportunity replay ────────────────────────────────────────
    st.markdown("### Blocked Opportunity Replay")
    st.caption("What happened after the agent blocked or skipped a candidate")

    try:
        from database.client import get_blocked_opportunities
        blocked = get_blocked_opportunities(days=7, limit=500)
        if blocked:
            df_b = pd.DataFrame(blocked)
            for col in ["max_favorable_pct", "max_adverse_pct", "close_after_pct",
                        "candidate_rank_score", "breakout_quality", "composite_score"]:
                if col in df_b.columns:
                    df_b[col] = pd.to_numeric(df_b[col], errors="coerce")
            checked = df_b[df_b.get("replay_checked_at").notna()] if "replay_checked_at" in df_b.columns else pd.DataFrame()
            missed = checked[checked["max_favorable_pct"].fillna(0) >= 0.5] if len(checked) else pd.DataFrame()
            saved = checked[checked["max_adverse_pct"].fillna(0) <= -0.35] if len(checked) else pd.DataFrame()

            b1, b2, b3, b4 = st.columns(4)
            b1.metric("Blocked/skipped (7d)", len(df_b))
            b2.metric("Replayed", len(checked))
            b3.metric("Potential misses", len(missed), delta="MFE ≥ 0.5%")
            b4.metric("Likely saves", len(saved), delta="MAE ≤ -0.35%")

            if len(checked):
                by_stage = (checked.groupby("block_stage", dropna=False)
                            .agg(
                                count=("ticker", "count"),
                                avg_mfe=("max_favorable_pct", "mean"),
                                avg_mae=("max_adverse_pct", "mean"),
                                avg_close=("close_after_pct", "mean"),
                            )
                            .reset_index()
                            .sort_values("avg_mfe", ascending=False))
                col_bo1, col_bo2 = st.columns([1, 1])
                with col_bo1:
                    fig_bo = px.bar(
                        by_stage,
                        x="block_stage",
                        y="avg_mfe",
                        color="block_stage",
                        text=by_stage["avg_mfe"].map(lambda v: f"{v:+.2f}%"),
                        template="plotly_dark",
                        labels={"block_stage": "Block stage", "avg_mfe": "Avg max favorable move (%)"},
                    )
                    fig_bo.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                                         plot_bgcolor="rgba(0,0,0,0)", height=260,
                                         margin=dict(l=0, r=0, t=10, b=0),
                                         showlegend=False)
                    st.plotly_chart(fig_bo, width="stretch")
                with col_bo2:
                    display_stage = by_stage.rename(columns={
                        "block_stage": "Stage",
                        "count": "Count",
                        "avg_mfe": "Avg MFE",
                        "avg_mae": "Avg MAE",
                        "avg_close": "Avg Close",
                    })
                    for pct_col in ["Avg MFE", "Avg MAE", "Avg Close"]:
                        display_stage[pct_col] = display_stage[pct_col].map(lambda v: f"{v:+.2f}%" if pd.notna(v) else "—")
                    st.dataframe(display_stage, width="stretch", hide_index=True)

                recent_cols = [
                    "created_at", "ticker", "action_hint", "block_stage", "block_reason",
                    "candidate_rank_score", "breakout_quality",
                    "max_favorable_pct", "max_adverse_pct", "close_after_pct",
                ]
                existing_cols = [c for c in recent_cols if c in checked.columns]
                recent = checked.sort_values("created_at", ascending=False)[existing_cols].head(25)
                st.markdown("##### Recent Replayed Blocks")
                st.dataframe(recent, width="stretch", hide_index=True)
            else:
                st.info("Blocked opportunities are being recorded. Replay metrics will appear after enough time has passed.")
        else:
            st.info("No blocked opportunities recorded yet.")
    except Exception as e:
        st.caption(f"Blocked opportunity stats unavailable: {e}")
