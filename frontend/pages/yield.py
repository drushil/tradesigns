"""frontend/pages/yield.py — Cash Sweep & Dividend Calendar dashboard."""
import os
import streamlit as st
import plotly.graph_objects as go
import pandas as pd
from datetime import datetime
from frontend.ui_help import column_config, metric, section_title
from frontend.ui_theme import page_header, status_pill


def render():
    broker_env = os.getenv("BROKER_ENV", "alpaca_paper")
    is_live    = broker_env == "ibkr_live"

    page_header(
        "Yield & Sweep",
        "Cash sweep simulation, dividend calendar, and live-mode readiness.",
        eyebrow="Cash Operations",
        pills=[
            status_pill("Live mode" if is_live else "Simulation mode", "positive" if is_live else "warning"),
            status_pill(broker_env, "info"),
        ],
    )

    if is_live:
        st.success("🟢 **LIVE MODE** — Cash sweeps execute via IBKR")
    else:
        st.warning("🟡 **SIMULATION MODE** — Sweeps are logged only, no real orders placed")

    # ── Section 1: Current sweep status ───────────────────────────────────────
    st.markdown("---")
    section_title("Current Sweep Status", level=3)

    try:
        from database.client import get_client
        db = get_client()
        result = (db.table("cash_sweeps")
                  .select("*")
                  .order("executed_at", desc=True)
                  .limit(1)
                  .execute())
        last_sweep = result.data[0] if result.data else None
    except Exception as e:
        st.error(f"Could not load sweep data: {e}")
        last_sweep = None

    if last_sweep:
        c1, c2, c3, c4 = st.columns(4)
        metric(c1, "Sweepable", f"€{last_sweep.get('sweepable_eur', 0):,.2f}")
        metric(c2, "Reserved", f"€{last_sweep.get('reserve_eur', 0):,.2f}")
        metric(c3, "Est. daily yield", f"€{last_sweep.get('est_daily_yield', 0):.4f}")
        metric(c4, "Est. annual yield", f"€{last_sweep.get('est_annual_yield', 0):,.2f}")

        sweep_ticker = last_sweep.get("sweep_ticker", "SGOV")
        skip_reason  = last_sweep.get("skip_reason") or last_sweep.get("mode", "—")
        st.info(
            f"**Sweep ticker:** `{sweep_ticker}` · "
            f"**Last status:** `{skip_reason}` · "
            f"**Computed:** {str(last_sweep.get('executed_at',''))[:19]} UTC"
        )
        if last_sweep.get("sim_note"):
            st.caption(f"📝 {last_sweep['sim_note']}")
    else:
        st.info("No sweep records yet. The nightly sweep runs at 21:05 UTC Mon-Fri.")

    # ── Section 2: Simulated yield accumulator ────────────────────────────────
    st.markdown("---")
    section_title("Simulated Yield Accumulator", level=3)

    try:
        from database.client import get_client
        db     = get_client()
        rows   = (db.table("cash_sweeps")
                  .select("executed_at,est_daily_yield,sweepable_eur,should_sweep,mode")
                  .eq("mode", "simulation")
                  .order("executed_at", desc=False)
                  .limit(500)
                  .execute())
        sweep_rows = rows.data or []
    except Exception:
        sweep_rows = []

    if sweep_rows:
        df = pd.DataFrame(sweep_rows)
        df["executed_at"]   = pd.to_datetime(df["executed_at"], utc=True, errors="coerce")
        df["est_daily_yield"] = pd.to_numeric(df["est_daily_yield"], errors="coerce").fillna(0)
        df["sweepable_eur"] = pd.to_numeric(df["sweepable_eur"], errors="coerce").fillna(0)

        # Sum yield only on eligible (should_sweep=True) rows
        eligible = df[df.get("should_sweep", True) == True] if "should_sweep" in df.columns else df
        total_sim_yield = eligible["est_daily_yield"].sum()
        first_date = df["executed_at"].min()
        avg_yield_pct = (
            (eligible["est_daily_yield"].mean() / eligible["sweepable_eur"].mean() * 365 * 100)
            if eligible["sweepable_eur"].mean() > 0 else 0
        )

        m1, m2, m3 = st.columns(3)
        metric(m1, "Total simulated yield", f"€{total_sim_yield:.4f}")
        metric(m2, "First sweep", str(first_date)[:10] if pd.notna(first_date) else "—")
        metric(m3, "Equiv. annual rate", f"{avg_yield_pct:.2f}%")

        # Cumulative yield chart
        df_sorted = df.sort_values("executed_at").copy()
        df_sorted["cum_yield"] = df_sorted["est_daily_yield"].cumsum()
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=df_sorted["executed_at"],
            y=df_sorted["cum_yield"],
            mode="lines",
            line=dict(color="#00d4a0", width=2),
            fill="tozeroy",
            fillcolor="rgba(0,212,160,0.06)",
            name="Cumulative simulated yield (€)",
        ))
        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=220,
            margin=dict(l=0, r=0, t=10, b=0),
            xaxis_title=None,
            yaxis_title="€",
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Simulated yield data will appear after the first nightly sweep run.")

    # ── Section 3: Dividend calendar ──────────────────────────────────────────
    st.markdown("---")
    section_title("Dividend Calendar", level=3)

    try:
        from database.client import get_client
        db      = get_client()
        div_rows = (db.table("dividend_opportunities")
                    .select("*")
                    .order("scanned_at", desc=True)
                    .limit(50)
                    .execute())
        div_data = div_rows.data or []
    except Exception:
        div_data = []

    # Deduplicate by ticker (keep most recent)
    seen, unique_divs = set(), []
    for row in div_data:
        if row.get("ticker") not in seen:
            seen.add(row.get("ticker"))
            unique_divs.append(row)

    if unique_divs:
        rows_display = []
        for row in unique_divs:
            score = float(row.get("opportunity_score", 0) or 0)
            if score >= 0.7:
                color = "🟢"
            elif score >= 0.4:
                color = "🟡"
            else:
                color = "⚪"
            rows_display.append({
                "":          color,
                "Ticker":    row.get("ticker", "—"),
                "Ex-Date":   row.get("next_ex_date", "—"),
                "Days Away": row.get("days_to_ex", "—"),
                "Yield %":   f"{row.get('dividend_yield', 0):.2f}%",
                "Score":     f"{score:.3f}",
                "Action":    row.get("action_taken", "logged_only"),
            })
        div_display = pd.DataFrame(rows_display)
        st.dataframe(
            div_display,
            use_container_width=True,
            hide_index=True,
            column_config=column_config(div_display.columns),
        )
    else:
        st.info("No upcoming dividends found in the ticker universe (yield > threshold, ex-date 1–5 days away).")
        st.caption(
            f"Scan enabled: `{os.getenv('DIVIDEND_SCAN_ENABLED','true')}` · "
            f"Min yield: `{os.getenv('DIVIDEND_MIN_YIELD_PCT','1.5')}%` · "
            f"Universe: `{os.getenv('TICKER_UNIVERSE','SPY,QQQ,GLD')}`"
        )

    # ── Section 4: Live mode readiness checklist ──────────────────────────────
    st.markdown("---")
    section_title("Live Mode Readiness", level=3)

    import importlib.util
    ibkr_built = importlib.util.find_spec("backend.broker.ibkr") is not None

    broker_live  = broker_env == "ibkr_live"
    sweep_xeon   = os.getenv("SWEEP_TICKER", "SGOV") == "XEON"

    def row(ok: bool, label: str, detail: str = ""):
        icon = "✅" if ok else "❌"
        st.markdown(f"{icon} **{label}**" + (f" — {detail}" if detail else ""))

    row(True,        "Sweep logic built",        "backend/sweep/agent.py")
    row(True,        "Dividend scanner built",   "backend/dividends/scanner.py")
    row(ibkr_built,  "IBKR broker module",       "backend/broker/ibkr.py — needed for live")
    row(broker_live, "BROKER_ENV = ibkr_live",   f"currently `{broker_env}`")
    row(sweep_xeon,  "SWEEP_TICKER = XEON",      f"currently `{os.getenv('SWEEP_TICKER','SGOV')}` (XEON for EU live trading)")
    st.markdown("❌ **IBKR account funded** — open at [ibkr.com](https://ibkr.com)")

    if not broker_live:
        st.caption(
            "When ready to go live: set `BROKER_ENV=ibkr_live` and `SWEEP_TICKER=XEON` in "
            "GitHub Variables and Streamlit Secrets. Build `backend/broker/ibkr.py` first."
        )
