"""frontend/pages/performance.py — Strategy performance metrics dashboard."""
import streamlit as st
import plotly.graph_objects as go

MIN_TRADES = 20


def _health_badge(sharpe, profit_factor, expectancy) -> tuple[str, str, str]:
    """Returns (label, bg_color, text_color) for the health badge."""
    red = (
        (sharpe is not None and sharpe < 0.5)
        or (profit_factor is not None and profit_factor < 1.0)
        or (expectancy is not None and expectancy < 0)
    )
    if red:
        return "RED", "#3a0a0a", "#ff5c5c"

    green = (
        sharpe is not None and sharpe >= 1.0
        and profit_factor is not None and profit_factor >= 1.5
        and expectancy is not None and expectancy >= 0.05
    )
    if green:
        return "GREEN", "#0a2e1f", "#00d4a0"

    return "AMBER", "#2e1f0a", "#EF9F27"


def render():
    st.title("📈 Performance Metrics")
    st.caption("Strategy health · All metrics computed from closed trades in Supabase")

    try:
        from database.client import get_recent_trades
        trades = get_recent_trades(days=90)
    except Exception as e:
        st.error(f"Could not load trades: {e}")
        return

    if len(trades) < MIN_TRADES:
        st.markdown(
            f"""
            <div style="
                background:#111;border:0.5px solid #222;border-radius:12px;
                padding:40px 32px;text-align:center;margin-top:32px
            ">
              <div style="font-size:48px;margin-bottom:16px">📊</div>
              <div style="font-size:20px;font-weight:500;color:#eee;margin-bottom:8px">
                Keep trading — need {MIN_TRADES} trades for reliable metrics
              </div>
              <div style="font-size:14px;color:#555">
                {len(trades)} trade{"s" if len(trades) != 1 else ""} recorded so far.
                Metrics appear once you have {MIN_TRADES}.
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    from backend.metrics.performance import (
        compute_expectancy, compute_profit_factor, compute_sharpe_ratio,
        compute_calmar_ratio, compute_rolling_sharpe, compute_signal_attribution,
        compute_r_multiples,
    )

    expectancy    = compute_expectancy(trades)
    profit_factor = compute_profit_factor(trades)
    sharpe        = compute_sharpe_ratio(trades)
    calmar        = compute_calmar_ratio(trades)
    r_data        = compute_r_multiples(trades)
    avg_r         = r_data["avg_r"]

    # ── Strategy health badge ──────────────────────────────────────────────────
    label, bg, fg = _health_badge(sharpe, profit_factor, expectancy)
    st.markdown(
        f"""
        <div style="
            display:inline-block;background:{bg};color:{fg};
            border:1.5px solid {fg};border-radius:8px;
            padding:10px 24px;font-size:15px;font-weight:600;
            letter-spacing:.1em;margin-bottom:24px
        ">
          ● STRATEGY HEALTH: {label}
        </div>
        <div style="font-size:11px;color:#555;margin-top:-18px;margin-bottom:24px">
          GREEN: Sharpe ≥ 1.0 &amp; PF ≥ 1.5 &amp; Expectancy ≥ 0.05% &nbsp;|&nbsp;
          AMBER: marginal &nbsp;|&nbsp;
          RED: Sharpe &lt; 0.5 or PF &lt; 1.0 or negative expectancy
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── KPI row ────────────────────────────────────────────────────────────────
    st.markdown('<div class="section-header">Key Metrics</div>', unsafe_allow_html=True)
    c1, c2, c3, c4, c5 = st.columns(5)

    def _fmt(val, fmt="+.4f", fallback="—"):
        return f"{val:{fmt}}" if val is not None else fallback

    def _pf_fmt(val):
        if val is None:
            return "—"
        if val == float("inf"):
            return "∞"
        return f"{val:.2f}×"

    c1.metric("Expectancy (%)", _fmt(expectancy, "+.4f"),
              help="Expected return per trade = win_rate×avg_win − loss_rate×avg_loss")
    c2.metric("Profit Factor", _pf_fmt(profit_factor),
              help="Gross profit / gross loss. >1.5 is healthy.")
    c3.metric("Sharpe Ratio", _fmt(sharpe, ".3f"),
              help="Annualized Sharpe on per-trade returns (×√252)")
    c4.metric("Calmar Ratio", _fmt(calmar, ".3f"),
              help="Annualized return / max drawdown")
    c5.metric("Avg R-Multiple", _fmt(avg_r, "+.3f"),
              help="Average R earned per trade (1R = initial stop distance)")

    st.markdown("---")

    # ── Rolling Sharpe chart ───────────────────────────────────────────────────
    st.markdown('<div class="section-header">Rolling Sharpe (20-trade window)</div>',
                unsafe_allow_html=True)

    rolling = compute_rolling_sharpe(trades, window=20)
    if rolling:
        x_vals = [p["trade_index"] + 1 for p in rolling]
        y_vals = [p["sharpe"] for p in rolling]
        colors = [
            "#00d4a0" if s >= 1.0 else ("#EF9F27" if s >= 0.5 else "#ff5c5c")
            for s in y_vals
        ]

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=x_vals, y=y_vals,
            mode="lines+markers",
            line=dict(color="#333", width=1.5),
            marker=dict(color=colors, size=7, line=dict(width=0)),
            name="Rolling Sharpe",
            hovertemplate="Trade #%{x}<br>Sharpe: %{y:.3f}<extra></extra>",
        ))
        fig.add_hline(y=1.0,  line_dash="dot",  line_color="#00d4a0",
                      annotation_text="1.0 (target)", annotation_font_color="#00d4a0")
        fig.add_hline(y=0.5,  line_dash="dot",  line_color="#EF9F27",
                      annotation_text="0.5 (min)",    annotation_font_color="#EF9F27")
        fig.add_hline(y=0.0,  line_dash="dash", line_color="#444")
        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="#0a0a0a",
            plot_bgcolor="#0a0a0a",
            height=280,
            margin=dict(l=0, r=0, t=8, b=8),
            xaxis=dict(title="Trade #", gridcolor="#1a1a1a"),
            yaxis=dict(title="Sharpe", gridcolor="#1a1a1a"),
            showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info(f"Need at least 20 closed trades for rolling Sharpe. ({len(trades)} so far)")

    st.markdown("---")

    # ── Signal attribution ─────────────────────────────────────────────────────
    st.markdown('<div class="section-header">Signal Attribution</div>',
                unsafe_allow_html=True)
    st.caption("Average (signal_score × trade_pnl) — positive = signal preceded profitable trades")

    attribution = compute_signal_attribution(trades)
    if attribution:
        sorted_attr = sorted(attribution.items(), key=lambda x: x[1], reverse=True)
        labels      = [k for k, _ in sorted_attr]
        values      = [v for _, v in sorted_attr]
        bar_colors  = ["#00d4a0" if v >= 0 else "#ff5c5c" for v in values]

        fig2 = go.Figure(go.Bar(
            x=values, y=labels,
            orientation="h",
            marker_color=bar_colors,
            hovertemplate="%{y}: %{x:.4f}<extra></extra>",
        ))
        fig2.add_vline(x=0, line_color="#444", line_width=1)
        fig2.update_layout(
            template="plotly_dark",
            paper_bgcolor="#0a0a0a",
            plot_bgcolor="#0a0a0a",
            height=max(200, len(labels) * 36),
            margin=dict(l=0, r=0, t=8, b=8),
            xaxis=dict(title="Avg contribution", gridcolor="#1a1a1a"),
            yaxis=dict(title="", gridcolor="#1a1a1a"),
        )
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("No signal attribution data yet — trades need a signals_json column populated.")

    st.markdown("---")

    # ── R-multiples distribution ───────────────────────────────────────────────
    st.markdown('<div class="section-header">R-Multiple Distribution</div>',
                unsafe_allow_html=True)
    r_vals = r_data.get("r_values", [])
    if r_vals:
        pos = r_data["positive_r"]
        neg = r_data["negative_r"]
        col_a, col_b, col_c = st.columns(3)
        col_a.metric("Positive R trades", f"{pos} ({pos/len(r_vals)*100:.0f}%)")
        col_b.metric("Negative R trades", f"{neg} ({neg/len(r_vals)*100:.0f}%)")
        col_c.metric("Avg R", f"{avg_r:+.3f}R" if avg_r is not None else "—")

        fig3 = go.Figure(go.Histogram(
            x=r_vals,
            nbinsx=30,
            marker_color="#00d4a0",
            opacity=0.8,
            name="R-multiples",
        ))
        fig3.add_vline(x=0, line_color="#ff5c5c", line_dash="dash", line_width=1.5)
        if avg_r is not None:
            fig3.add_vline(x=avg_r, line_color="#EF9F27", line_dash="dot",
                           annotation_text=f"avg {avg_r:+.2f}R",
                           annotation_font_color="#EF9F27")
        fig3.update_layout(
            template="plotly_dark",
            paper_bgcolor="#0a0a0a",
            plot_bgcolor="#0a0a0a",
            height=220,
            margin=dict(l=0, r=0, t=8, b=8),
            xaxis=dict(title="R-multiple", gridcolor="#1a1a1a"),
            yaxis=dict(title="Trades",     gridcolor="#1a1a1a"),
            showlegend=False,
        )
        st.plotly_chart(fig3, use_container_width=True)
