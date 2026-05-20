"""frontend/pages/performance.py — Strategy performance metrics dashboard."""
import os
import streamlit as st
import plotly.graph_objects as go
import pandas as pd
from frontend.ui_help import column_config, metric, section_title
from frontend.ui_theme import page_header, panel_html, status_pill

MIN_TRADES = 20


def render():
    try:
        from database.client import get_recent_trades
        trades = get_recent_trades(days=90)
    except Exception as e:
        st.error(f"Could not load trades: {e}")
        return

    if len(trades) < MIN_TRADES:
        page_header(
            "Performance Metrics",
            "Strategy health and validation metrics from closed trades in Supabase.",
            eyebrow="Risk Analytics",
            pills=[status_pill(f"{len(trades)}/{MIN_TRADES} trades", "warning")],
        )
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
              <div style="font-size:14px;color:#555;margin-bottom:20px">
                {len(trades)} trade{"s" if len(trades) != 1 else ""} recorded so far.
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.progress(len(trades) / MIN_TRADES)
        return

    from backend.metrics.performance import (
        compute_expectancy, compute_profit_factor, compute_sharpe_ratio,
        compute_calmar_ratio, compute_rolling_sharpe, compute_signal_attribution,
        compute_r_multiples, compute_strategy_health,
    )
    try:
        from backend.metrics.validation import (
            walk_forward_splits, evaluate_score_thresholds,
            signal_information_coefficients,
        )
        has_validation = True
    except ImportError:
        has_validation = False

    health        = compute_strategy_health(trades)
    exp_data      = compute_expectancy(trades)
    profit_factor = compute_profit_factor(trades)
    sharpe        = compute_sharpe_ratio(trades)
    calmar        = compute_calmar_ratio(trades)
    r_data        = compute_r_multiples(trades)
    avg_r         = r_data["avg_r"]
    expectancy    = exp_data["expectancy_pct"] if exp_data else None
    win_rate      = exp_data["win_rate"]        if exp_data else None

    page_header(
        "Performance Metrics",
        "Strategy health, expectancy, R-multiple behavior, costs, and validation checks.",
        eyebrow="Risk Analytics",
        pills=[
            status_pill(f"{len(trades)} trades", "info"),
            status_pill(f"{health['status']} health", "positive" if health["status"] == "GREEN" else ("warning" if health["status"] == "AMBER" else "negative")),
        ],
    )

    # ── Section 1: Strategy Health Badge ──────────────────────────────────────
    _STATUS_COLORS = {
        "GREEN":             ("#0a2e1f", "#00d4a0"),
        "AMBER":             ("#2e1f0a", "#EF9F27"),
        "RED":               ("#3a0a0a", "#ff5c5c"),
        "INSUFFICIENT_DATA": ("#111",    "#888"),
    }
    status = health["status"]
    bg, fg = _STATUS_COLORS.get(status, ("#111", "#888"))
    st.markdown(
        f"""
        <div style="
            background:{bg};color:{fg};
            border:1.5px solid {fg};border-radius:8px;
            padding:14px 24px;font-size:16px;font-weight:700;
            letter-spacing:.08em;margin-bottom:8px
        ">
          ● STRATEGY HEALTH: {status}
        </div>
        <div style="font-size:13px;color:#888;margin-bottom:4px">{health['message']}</div>
        """,
        unsafe_allow_html=True,
    )
    for issue in health.get("issues", []):
        st.warning(issue)

    st.markdown("---")

    # ── Section 2: KPI Row ────────────────────────────────────────────────────
    def _fmt(val, fmt=".3f", fallback="—"):
        return f"{val:{fmt}}" if val is not None else fallback

    c1, c2, c3, c4, c5 = st.columns(5)
    metric(
        c1,
        "Expectancy",
        f"{expectancy:+.4f}%" if expectancy is not None else "—",
        delta="▲ above 0.15% target" if (expectancy or 0) > 0.15 else "▼ below 0.15% target",
        delta_color="normal" if (expectancy or 0) > 0.15 else "inverse",
    )
    metric(
        c2,
        "Profit Factor",
        f"{profit_factor:.2f}×" if profit_factor is not None else "—",
        delta="▲ > 1.3" if (profit_factor or 0) > 1.3 else "▼ < 1.3",
        delta_color="normal" if (profit_factor or 0) > 1.3 else "inverse",
    )
    metric(
        c3,
        "Sharpe Ratio",
        _fmt(sharpe),
        delta="▲ > 0.8" if (sharpe or 0) > 0.8 else "▼ < 0.8",
        delta_color="normal" if (sharpe or 0) > 0.8 else "inverse",
    )
    metric(
        c4,
        "Calmar Ratio",
        _fmt(calmar),
        delta="▲ > 1.0" if (calmar or 0) > 1.0 else "▼ < 1.0",
        delta_color="normal" if (calmar or 0) > 1.0 else "inverse",
    )
    metric(
        c5,
        "Avg R-Multiple",
        f"{avg_r:+.3f}R" if avg_r is not None else "—",
        delta="▲ > 1.0R" if (avg_r or 0) > 1.0 else "▼ < 1.0R",
        delta_color="normal" if (avg_r or 0) > 1.0 else "inverse",
    )

    st.markdown("---")

    # ── Section 3: Rolling Sharpe Chart ───────────────────────────────────────
    section_title("Rolling Sharpe (20-trade window)", help_key="Rolling risk-adjusted return over the last 20 closed trades.")
    rolling = compute_rolling_sharpe(trades, window=20)
    if rolling:
        x_vals    = [p["trade_index"] + 1 for p in rolling]
        y_vals    = [p["sharpe"] for p in rolling]
        warn_idxs = [p["trade_index"] + 1 for p in rolling if p.get("degradation_warning")]
        colors    = [
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
        if warn_idxs:
            warn_y = [y_vals[x_vals.index(i)] for i in warn_idxs if i in x_vals]
            fig.add_trace(go.Scatter(
                x=warn_idxs, y=warn_y,
                mode="markers",
                marker=dict(symbol="x", color="#ff5c5c", size=12),
                name="Degradation warning",
                hovertemplate="Trade #%{x}<br>⚠ Degradation<extra></extra>",
            ))
        fig.add_hline(y=1.0, line_dash="dot", line_color="#00d4a0",
                      annotation_text="1.0 (target)", annotation_font_color="#00d4a0")
        fig.add_hline(y=0.5, line_dash="dot", line_color="#EF9F27",
                      annotation_text="0.5 (min)",   annotation_font_color="#EF9F27")
        fig.add_hline(y=0.0, line_dash="dash", line_color="#444")
        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=280,
            margin=dict(l=0, r=0, t=8, b=8),
            xaxis=dict(title="Trade #", gridcolor="#1a1a1a"),
            yaxis=dict(title="Sharpe",  gridcolor="#1a1a1a"),
        )
        st.plotly_chart(fig, width="stretch")
    else:
        st.info(f"Need at least 20 closed trades for rolling Sharpe. ({len(trades)} so far)")

    st.markdown("---")

    # ── Section 4: Attribution + R-Multiple ───────────────────────────────────
    col_attr, col_r = st.columns(2)

    with col_attr:
        section_title("Signal Attribution", help_key="Signals with score > 0.3; net attribution equals influenced wins minus influenced losses.")
        attribution = compute_signal_attribution(trades)
        if attribution:
            labels     = [r["signal"] for r in attribution]
            values     = [r["net_attribution"] for r in attribution]
            hover_text = [
                f"Win rate: {r['win_rate_when_active']:.0%} · avg P&L: {r['avg_pnl_when_active']:+.3f}%"
                for r in attribution
            ]
            bar_colors = ["#00d4a0" if v >= 0 else "#ff5c5c" for v in values]
            fig2 = go.Figure(go.Bar(
                x=values, y=labels,
                orientation="h",
                marker_color=bar_colors,
                text=hover_text,
                textposition="outside",
                hovertemplate="%{y}<br>%{text}<extra></extra>",
            ))
            fig2.add_vline(x=0, line_color="#444", line_width=1)
            fig2.update_layout(
                template="plotly_dark",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                height=max(200, len(labels) * 36),
                margin=dict(l=0, r=140, t=8, b=8),
                xaxis=dict(title="Net attribution", gridcolor="#1a1a1a"),
                yaxis=dict(title="", gridcolor="#1a1a1a"),
                showlegend=False,
            )
            st.plotly_chart(fig2, width="stretch")
        else:
            st.info("No attribution data — trades need signals_json populated.")

    with col_r:
        section_title("R-Multiple Distribution", help_key="Distribution of realised returns measured in units of initial risk.")
        r_vals = r_data.get("r_values", [])
        if r_vals:
            pos = r_data["positive_r"]
            neg = r_data["negative_r"]
            m1, m2, m3 = st.columns(3)
            metric(m1, "Positive R", f"{pos} ({pos/len(r_vals):.0%})")
            metric(m2, "Negative R", f"{neg} ({neg/len(r_vals):.0%})")
            metric(m3, "Avg R", f"{avg_r:+.2f}R" if avg_r is not None else "—")

            fig3 = go.Figure(go.Histogram(
                x=r_vals,
                nbinsx=30,
                marker_color=[("#00d4a0" if r > 0 else "#ff5c5c") for r in r_vals],
                opacity=0.8,
            ))
            fig3.add_vline(x=1.0, line_color="#EF9F27", line_dash="dot",
                           annotation_text="R=1.0", annotation_font_color="#EF9F27")
            fig3.add_vline(x=0.0, line_color="#ff5c5c", line_dash="dash", line_width=1)
            if avg_r is not None:
                fig3.add_vline(x=avg_r, line_color="#EF9F27", line_dash="dot",
                               annotation_text=f"avg {avg_r:+.2f}R",
                               annotation_font_color="#EF9F27")
            fig3.update_layout(
                template="plotly_dark",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                height=220,
                margin=dict(l=0, r=0, t=8, b=8),
                xaxis=dict(title="R-multiple", gridcolor="#1a1a1a"),
                yaxis=dict(title="Trades",     gridcolor="#1a1a1a"),
                showlegend=False,
            )
            st.plotly_chart(fig3, width="stretch")
        else:
            st.info("No R-multiple data yet.")

    st.markdown("---")

    # ── Section 5: Per-Regime Performance Table ───────────────────────────────
    section_title("Performance by Regime", help_key="Performance by Market Regime")
    df_t = pd.DataFrame(trades)
    if "regime" in df_t.columns and "net_pnl_pct" in df_t.columns and not df_t.empty:
        regime_groups = df_t.groupby("regime")
        rows = []
        for regime_name, grp in regime_groups:
            wins  = (grp["net_pnl_pct"] > 0).sum()
            total = len(grp)
            gross_win  = grp.loc[grp["net_pnl_pct"] > 0, "net_pnl_pct"].sum()
            gross_loss = abs(grp.loc[grp["net_pnl_pct"] <= 0, "net_pnl_pct"].sum())
            pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else None
            rows.append({
                "Regime":       regime_name,
                "Trades":       total,
                "Win Rate":     f"{wins/total:.0%}" if total else "—",
                "Avg P&L":      f"{grp['net_pnl_pct'].mean():+.3f}%",
                "Profit Factor": f"{pf:.2f}" if pf else "—",
                "Avg Hold (min)": f"{grp['hold_minutes'].mean():.0f}" if "hold_minutes" in grp else "—",
            })
        if rows:
            df_regime = pd.DataFrame(rows)
            best_idx  = df_t.groupby("regime")["net_pnl_pct"].mean().idxmax()
            st.dataframe(
                df_regime.style.apply(
                    lambda row: ["background-color:#0a2e1f" if row["Regime"] == best_idx else "" for _ in row],
                    axis=1,
                ),
                use_container_width=True, hide_index=True,
                column_config=column_config(df_regime.columns),
            )
    else:
        st.info("Not enough regime data yet.")

    st.markdown("---")

    # ── Section 6: Cost Analysis ──────────────────────────────────────────────
    section_title("Cost Analysis", help_key="Estimated execution, model, and slippage costs compared with gross winning P&L.")
    total_trades   = len(trades)
    est_commission = total_trades * 1.25
    total_llm_cost = sum(t.get("llm_cost_eur", 0) or 0 for t in trades)
    total_slippage = sum(t.get("slippage_eur",  0) or 0 for t in trades)
    gross_pnl_eur  = sum(
        t.get("pnl_eur", 0) or 0 for t in trades if (t.get("pnl_eur") or 0) > 0
    )
    total_costs    = est_commission + total_llm_cost + total_slippage
    cost_pct_gross = total_costs / gross_pnl_eur * 100 if gross_pnl_eur > 0 else None

    cc1, cc2, cc3, cc4 = st.columns(4)
    metric(cc1, "Est. Commission",  f"€{est_commission:.2f}",
           help=f"€1.25 × {total_trades} trades (estimate)")
    metric(cc2, "LLM Costs",        f"€{total_llm_cost:.4f}")
    metric(cc3, "Est. Slippage",    f"€{total_slippage:.2f}")
    metric(cc4, "Costs / Gross P&L",
           f"{cost_pct_gross:.1f}%" if cost_pct_gross is not None else "—")

    start_eur       = float(os.getenv("STARTING_CAPITAL_EUR", "3000"))
    monthly_alpha   = total_costs / max(1, total_trades / 22) if total_trades else 0
    st.caption(
        f"Minimum monthly alpha needed to cover costs: €{monthly_alpha:.2f} "
        f"({monthly_alpha/start_eur*100:.3f}% of €{start_eur:,.0f} starting capital)"
    )

    # ── Validation checks (optional) ─────────────────────────────────────────
    if has_validation:
        st.markdown("---")
        section_title("Validation Checks", help_key="Retrospective checks to review before promoting thresholds or weights.")
        splits     = walk_forward_splits(trades, train_size=40, test_size=20)
        thresholds = evaluate_score_thresholds(trades)
        ics        = signal_information_coefficients(trades)

        v1, v2 = st.columns(2)
        with v1:
            if splits:
                last = splits[-1]
                metric(st, "Latest test expectancy",
                       f"{last['test']['expectancy_pct']:+.4f}%")
                st.caption(
                    f"{last['test']['trade_count']} out-of-sample trades · "
                    f"train expectancy {last['train']['expectancy_pct']:+.4f}%"
                )
            else:
                st.info("Need at least 60 trades for walk-forward validation.")
        with v2:
            viable = [r for r in thresholds if r["trade_count"] >= 8 and r["expectancy_pct"] is not None]
            if viable:
                best = max(viable, key=lambda r: r["expectancy_pct"])
                metric(st, "Best observed threshold",
                       f"|score| ≥ {best['threshold']:.2f}",
                       f"{best['expectancy_pct']:+.4f}% exp.")
            else:
                st.info("Need more trades to compare score thresholds.")
        if ics:
            ic_rows = sorted(ics.items(), key=lambda item: item[1], reverse=True)
            st.caption("Signal IC: " + " · ".join(
                f"{name} {value:+.3f}" for name, value in ic_rows[:5]
            ))
