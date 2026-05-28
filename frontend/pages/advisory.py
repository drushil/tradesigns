"""frontend/pages/advisory.py — Advisory feed + on-demand ticker check.

Shows the recent Discord-style advisory cards that came out of run_advisory_cycle()
and lets the user re-run the scan logic for any ticker on demand.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import streamlit as st

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

from frontend.ui_help import button, column_config, metric
from frontend.ui_theme import (
    metric_card,
    page_header,
    status_pill,
)


STAGE_TONE = {
    "trade": "positive",
    "watch": "warning",
    "ignition": "info",
}

STAGE_LABEL = {
    "trade": "🟢 TRADE",
    "watch": "🟡 WATCH",
    "ignition": "🔥 IGNITION",
}


def _local_tz():
    if ZoneInfo is None:
        return timezone.utc
    return ZoneInfo(os.getenv("DASHBOARD_TIMEZONE", "Europe/Berlin"))


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _stage_of(row: dict) -> str:
    return str(
        row.get("alert_stage")
        or _as_dict(row.get("signal_json")).get("alert_stage")
        or "trade"
    )


def _format_time(ts) -> str:
    tz = _local_tz()
    try:
        parsed = pd.to_datetime(ts, utc=True, errors="coerce")
        if pd.isna(parsed):
            return "—"
        return parsed.tz_convert(tz).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts) if ts else "—"


def _symbol_catalog(advisory_mod, rows: list[dict] = None,
                    market: str = None) -> tuple[list[str], dict[str, str]]:
    labels: dict[str, str] = {}
    for market_name, market_items in advisory_mod.ADVISORY_UNIVERSE.items():
        if market and market_name != market:
            continue
        for item in market_items:
            symbol = str(item.get("data_symbol") or "").upper()
            if not symbol:
                continue
            name = item.get("broker_display_name") or symbol
            labels[symbol] = f"{symbol} — {name}"

    for row in rows or []:
        symbol = str(row.get("data_symbol") or "").upper()
        if not symbol:
            continue
        if market and str(row.get("market") or "").upper() != market:
            continue
        name = row.get("broker_display_name") or symbol
        labels.setdefault(symbol, f"{symbol} — {name}")

    return sorted(labels), labels


def _select_index(options: list[str], preferred: str, fallback: str = None) -> int:
    preferred = str(preferred or "").upper()
    fallback = str(fallback or "").upper()
    if preferred in options:
        return options.index(preferred)
    if fallback in options:
        return options.index(fallback)
    return 0


def render():
    from database.client import get_recent_advisory_signals
    from backend import advisory

    cfg = advisory.load_config()
    rows = get_recent_advisory_signals(days=1, limit=400) or []

    today_local = datetime.now(_local_tz()).date()
    today_rows = [
        r for r in rows
        if _format_time(r.get("created_at")).startswith(str(today_local))
    ]
    by_stage: dict[str, int] = {"ignition": 0, "watch": 0, "trade": 0}
    for r in today_rows:
        stage = _stage_of(r)
        if stage in by_stage:
            by_stage[stage] += 1
    last_alert_ts = today_rows[0].get("created_at") if today_rows else None

    page_header(
        "Advisory",
        "Trade Republic-friendly cards with EUR levels, ignition pings, "
        "tier escalation and on-demand ticker checks.",
        eyebrow="Discord Alerts",
        pills=[
            status_pill(f"{len(today_rows)} alerts today", "info"),
            status_pill(
                f"FX {cfg.fx_rate:.4f} ({cfg.fx_rate_source})"
                if cfg.fx_rate else "FX unavailable",
                "neutral",
            ),
            status_pill(
                f"Last: {_format_time(last_alert_ts)}" if last_alert_ts else "No alerts today",
                "positive" if last_alert_ts else "neutral",
            ),
        ],
    )

    c1, c2, c3, c4 = st.columns(4)
    metric_card(c1, "Alerts today", len(today_rows), tone="info")
    metric_card(c2, "🔥 Ignitions", by_stage["ignition"], tone="info")
    metric_card(c3, "🟡 Watches", by_stage["watch"], tone="warning")
    metric_card(c4, "🟢 Trades", by_stage["trade"], tone="positive")

    st.divider()

    # ── Replay Scoreboard ──────────────────────────────────────────────────
    _render_scoreboard()

    st.divider()

    # ── On-demand ticker scan ───────────────────────────────────────────────
    st.subheader("🔍 Check a ticker on demand")
    st.caption(
        "Runs the advisory scan pipeline against the chosen symbol right now — "
        "same logic the cron runs every 5 min. Does not send to Discord and does "
        "not affect daily alert caps."
    )

    c_market, c_sym, c_market_spacer, c_side, c_btn = st.columns([1, 2, 1, 1, 1])
    with c_market:
        market = st.selectbox("Market", ["US", "EU"], index=0, key="advisory_check_market")
    symbol_options, symbol_labels = _symbol_catalog(advisory, rows=rows, market=market)
    with c_sym:
        symbol = st.selectbox(
            "Symbol",
            symbol_options,
            index=_select_index(
                symbol_options,
                st.session_state.get("advisory_check_symbol", "AMZN"),
                "AMZN" if market == "US" else "SAP.DE",
            ),
            format_func=lambda s: symbol_labels.get(s, s),
            key=f"advisory_check_symbol_select_{market}",
        )
    with c_market_spacer:
        st.caption("Ticker universe")
        selected_item = _find_universe_item(advisory, symbol) or {}
        st.write(selected_item.get("exchange") or market)
    with c_side:
        side_choice = st.selectbox("Side", ["auto", "BUY", "SELL"], index=0, key="advisory_check_side")
    with c_btn:
        st.write("")  # vertical alignment
        st.write("")
        run_now = button("Run scan", width="stretch")

    if run_now and symbol:
        st.session_state["advisory_check_symbol"] = symbol
        with st.spinner(f"Scanning {symbol}…"):
            result = _preview_advisory(advisory, cfg, symbol, market, side_choice)
        _render_preview(result, symbol)

    st.divider()

    # ── Recent alert feed ──────────────────────────────────────────────────
    st.subheader("📡 Recent alerts (last 24h)")
    if not rows:
        st.info("No advisory signals stored yet. Once the cron runs, alerts will appear here.")
        return

    stage_options = ["all", "trade", "watch", "ignition"]
    mode_options = ["all", "live", "shadow"]

    c_stage, c_mode, c_symbol = st.columns([2, 2, 2])
    with c_stage:
        stage_filter = st.radio("Stage", stage_options, index=0, horizontal=True,
                                key="advisory_stage_filter")
    with c_mode:
        mode_filter = st.radio("Mode", mode_options, index=0, horizontal=True,
                               key="advisory_mode_filter")
    with c_symbol:
        feed_symbols, feed_labels = _symbol_catalog(advisory, rows=rows)
        symbol_filter = st.selectbox(
            "Symbol",
            ["__all__"] + feed_symbols,
            index=0,
            format_func=lambda s: "All symbols" if s == "__all__" else feed_labels.get(s, s),
            key="advisory_symbol_filter",
        )

    filtered = []
    for r in rows:
        if stage_filter != "all" and _stage_of(r) != stage_filter:
            continue
        if mode_filter != "all" and str(r.get("mode")) != mode_filter:
            continue
        if symbol_filter != "__all__" and symbol_filter != str(r.get("data_symbol", "")).upper():
            continue
        filtered.append(r)

    if not filtered:
        st.info("No rows match the current filters.")
        return

    table = pd.DataFrame([
        {
            "time": _format_time(r.get("created_at")),
            "symbol": r.get("data_symbol"),
            "name": r.get("broker_display_name"),
            "stage": STAGE_LABEL.get(_stage_of(r), _stage_of(r)),
            "side": r.get("side"),
            "grade": r.get("grade"),
            "composite": float(r.get("composite_score") or 0),
            "ev_pct": float(r.get("ev_net_pct") or 0) if r.get("ev_net_pct") is not None else None,
            "breakout": float(r.get("breakout_quality") or 0) if r.get("breakout_quality") is not None else None,
            "status": r.get("status"),
            "mode": r.get("mode"),
        }
        for r in filtered
    ])
    st.dataframe(
        table,
        width="stretch",
        hide_index=True,
        column_config=column_config(table.columns),
    )

    st.markdown("**Expand an alert below to see the full Discord card.**")
    for r in filtered[:25]:
        stage = _stage_of(r)
        title = (
            f"{_format_time(r.get('created_at'))} · "
            f"{r.get('data_symbol')} {r.get('side')} · "
            f"{STAGE_LABEL.get(stage, stage)} · "
            f"grade {r.get('grade') or '—'} · "
            f"composite {float(r.get('composite_score') or 0):.2f}"
        )
        with st.expander(title, expanded=False):
            msg = r.get("message_text")
            if msg:
                st.code(msg, language="markdown")
            else:
                st.caption("No stored message_text for this row.")
            extras = {
                "ignition_json": _as_dict(r.get("ignition_json")),
                "late_chase_json": _as_dict(r.get("late_chase_json")),
                "pullback_confirmed": r.get("pullback_confirmed"),
                "valid_until": r.get("valid_until"),
                "fx_rate": r.get("fx_rate"),
            }
            non_empty = {k: v for k, v in extras.items() if v not in (None, {}, "", False)}
            if non_empty:
                st.json(non_empty)


# ── Replay Scoreboard ───────────────────────────────────────────────────────

def _fmt_ret(val) -> str:
    """Format a forward-return value as a coloured +/-% string with emoji."""
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "—"
    if v >= 0.5:
        return f"🟢 {v:+.2f}%"
    if v >= 0.1:
        return f"🟡 {v:+.2f}%"
    if v >= -0.1:
        return f"⬜ {v:+.2f}%"
    return f"🔴 {v:+.2f}%"


def _fmt_win(rate_pct) -> str:
    if pd.isna(rate_pct):
        return "—"
    r = float(rate_pct)
    if r >= 60:
        return f"🟢 {r:.0f}%"
    if r >= 45:
        return f"🟡 {r:.0f}%"
    return f"🔴 {r:.0f}%"


def _render_scoreboard():
    """Grade × Stage forward-return scoreboard section."""
    try:
        import plotly.express as px
    except ImportError:
        px = None

    st.subheader("📊 Replay Scoreboard")
    st.caption(
        "Forward-return performance of advisory alerts, scored automatically "
        "5 / 15 / 30 / 60 min after each alert fires. "
        "Green = profitable on average at that horizon."
    )

    c_days, c_mode, c_market, _ = st.columns([2, 2, 2, 4])
    with c_days:
        days_back = st.radio(
            "Window", [7, 14, 30, 90], index=1,
            horizontal=True, key="sb_days_back",
            format_func=lambda x: f"{x}d",
        )
    with c_mode:
        sb_mode = st.radio(
            "Mode", ["all", "live", "shadow"], index=0,
            horizontal=True, key="sb_mode",
        )
    with c_market:
        sb_market = st.radio(
            "Market", ["all", "US", "EU"], index=0,
            horizontal=True, key="sb_market",
        )

    from database.client import get_advisory_scoreboard
    sb_rows = get_advisory_scoreboard(
        days_back=days_back,
        mode=None if sb_mode == "all" else sb_mode,
        market=None if sb_market == "all" else sb_market,
    )

    if not sb_rows:
        st.info(
            "No scored alerts yet — forward returns are computed automatically "
            "during each agent cycle (min_age=5 min, max_age=4 days)."
        )
        return

    df = pd.DataFrame(sb_rows)
    for col in ["forward_return_5m", "forward_return_15m",
                "forward_return_30m", "forward_return_60m",
                "max_favorable_pct", "max_adverse_pct",
                "composite_score", "breakout_quality"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    total_scored = len(df)
    r15_all = df["forward_return_15m"].dropna()
    win_rate_15m = float((r15_all > 0).mean() * 100) if len(r15_all) else float("nan")
    avg_15m = float(r15_all.mean()) if len(r15_all) else float("nan")
    best_grade = (
        df.loc[df["forward_return_15m"] == df["forward_return_15m"].max(), "grade"].iloc[0]
        if "grade" in df.columns and not df["forward_return_15m"].isna().all()
        else "—"
    )

    c1, c2, c3, c4 = st.columns(4)
    metric_card(c1, "Scored alerts", total_scored, tone="info")
    metric_card(
        c2, "Win rate 15m",
        f"{win_rate_15m:.0f}%" if not pd.isna(win_rate_15m) else "—",
        tone="positive" if win_rate_15m >= 55 else (
            "warning" if win_rate_15m >= 45 else "negative"
        ) if not pd.isna(win_rate_15m) else "neutral",
    )
    metric_card(
        c3, "Avg return 15m",
        f"{avg_15m:+.2f}%" if not pd.isna(avg_15m) else "—",
        tone="positive" if (not pd.isna(avg_15m) and avg_15m > 0.1) else (
            "warning" if (not pd.isna(avg_15m) and avg_15m > -0.1) else "negative"
        ),
    )
    metric_card(c4, "Coverage", f"{days_back}d", tone="neutral")

    # ── Grade × Stage breakdown ──────────────────────────────────────────
    st.markdown("**Grade × Stage breakdown**")

    if "alert_stage" not in df.columns:
        df["alert_stage"] = "trade"
    if "grade" not in df.columns:
        df["grade"] = "—"

    grade_order = {"A+": 0, "A": 1, "B": 2, "C": 3}
    agg_rows = []
    for (grade, stage), grp in df.groupby(["grade", "alert_stage"], dropna=False):
        r5  = grp["forward_return_5m"].dropna()
        r15 = grp["forward_return_15m"].dropna()
        r30 = grp["forward_return_30m"].dropna()
        r60 = grp["forward_return_60m"].dropna()
        mfe = grp["max_favorable_pct"].dropna() if "max_favorable_pct" in grp.columns else pd.Series(dtype=float)
        mae = grp["max_adverse_pct"].dropna() if "max_adverse_pct" in grp.columns else pd.Series(dtype=float)
        win15 = float((r15 > 0).mean() * 100) if len(r15) >= 3 else float("nan")
        mfe_mae_str = (
            f"{mfe.mean():.1f}× / {mae.mean():.1f}×"
            if len(mfe) >= 2 and len(mae) >= 2
            else "—"
        )
        agg_rows.append({
            "grade": str(grade) if grade is not None else "—",
            "stage": STAGE_LABEL.get(str(stage), str(stage)),
            "n": len(grp),
            "avg 5m": _fmt_ret(r5.mean() if len(r5) else None),
            "avg 15m": _fmt_ret(r15.mean() if len(r15) else None),
            "avg 30m": _fmt_ret(r30.mean() if len(r30) else None),
            "avg 60m": _fmt_ret(r60.mean() if len(r60) else None),
            "win % 15m": _fmt_win(win15),
            "MFE/MAE": mfe_mae_str,
            "_sort": (grade_order.get(str(grade) if grade else "—", 99),
                      str(stage) if stage else ""),
        })

    if agg_rows:
        agg_df = (
            pd.DataFrame(agg_rows)
            .sort_values("_sort", key=lambda s: s.map(str))
            .drop(columns=["_sort"])
            .reset_index(drop=True)
        )
        st.dataframe(agg_df, hide_index=True, use_container_width=True)
    else:
        st.info("Not enough data to build the breakdown table yet.")

    # ── Daily trend chart ─────────────────────────────────────────────────
    if (px is not None
            and "created_at" in df.columns
            and not df["forward_return_15m"].isna().all()):
        df["_date"] = pd.to_datetime(df["created_at"], utc=True, errors="coerce").dt.date
        daily = (
            df.groupby("_date")["forward_return_15m"]
            .agg(avg_15m="mean", alerts="count")
            .reset_index()
            .rename(columns={"_date": "date"})
        )
        if len(daily) >= 2:
            st.markdown("**Daily average 15m return**")
            fig = px.bar(
                daily, x="date", y="avg_15m",
                color="avg_15m",
                color_continuous_scale=["#ef4444", "#6b7280", "#22c55e"],
                color_continuous_midpoint=0,
                labels={"avg_15m": "Avg 15m return (%)", "date": ""},
                template="plotly_dark",
                height=220,
                hover_data={"alerts": True, "avg_15m": ":.2f"},
            )
            fig.update_layout(
                showlegend=False,
                coloraxis_showscale=False,
                margin=dict(t=16, b=28, l=0, r=0),
            )
            st.plotly_chart(fig, use_container_width=True)

    # ── Ticker breakdown ──────────────────────────────────────────────────
    if "data_symbol" in df.columns:
        with st.expander("Ticker breakdown", expanded=False):
            sym_agg = (
                df.groupby("data_symbol")["forward_return_15m"]
                .agg(n="count", avg_15m="mean",
                     win_pct=lambda x: float((x.dropna() > 0).mean() * 100) if x.dropna().any() else float("nan"))
                .reset_index()
                .sort_values("avg_15m", ascending=False)
            )
            sym_agg["avg_15m"] = sym_agg["avg_15m"].apply(
                lambda v: _fmt_ret(v) if not pd.isna(v) else "—"
            )
            sym_agg["win_pct"] = sym_agg["win_pct"].apply(
                lambda v: _fmt_win(v) if not pd.isna(v) else "—"
            )
            sym_agg = sym_agg.rename(columns={
                "data_symbol": "symbol",
                "avg_15m": "avg 15m return",
                "win_pct": "win %",
            })
            st.dataframe(sym_agg, hide_index=True, use_container_width=True)


# ── On-demand preview ───────────────────────────────────────────────────────

def _preview_advisory(advisory_mod, cfg, symbol: str, market: str, side_choice: str) -> dict:
    """Build a candidate from the live signal pipeline without touching DB or Discord."""
    item = _find_universe_item(advisory_mod, symbol) or {
        "data_symbol": symbol,
        "broker_display_name": symbol,
        "exchange": "NASDAQ" if market == "US" else "Xetra",
        "currency": "USD" if market == "US" else "EUR",
    }
    market = "US" if item.get("currency") == "USD" else market

    weights = advisory_mod._weights_for_market(market, listing_type=item.get("listing_type"))
    try:
        regime_state = advisory_mod.detect_regime(symbol)
    except Exception as e:
        return {"error": f"detect_regime failed: {e}"}

    try:
        signal_result = advisory_mod.compute_all_signals(symbol, weights, regime_state=regime_state)
    except Exception as e:
        return {"error": f"compute_all_signals failed: {e}"}

    composite = float(signal_result.get("composite_score") or 0)
    signals = signal_result.get("signals") or {}
    atr_data = signal_result.get("atr_data") or {}

    if side_choice == "auto":
        side = "BUY" if composite >= 0 else "SELL"
    else:
        side = side_choice

    breakout = advisory_mod._breakout_quality(
        side, composite, signals, getattr(regime_state, "market_regime", "")
    )
    orb_active = bool((signals.get("orb") or {}).get("meta", {}).get("active"))
    grade = advisory_mod._grade(composite, breakout, orb_active)

    late_chase = advisory_mod._late_chase_block(
        side, signals, atr_data,
        {
            "late_chase_block_enabled": True,
            "late_chase_atr_mult": advisory_mod._env_float("ADVISORY_LATE_CHASE_ATR_MULT", 1.5),
        },
    ) or {}
    ignition = advisory_mod._ignition_check(symbol, side, composite, atr_data) or {}

    quality = advisory_mod._data_quality(
        symbol, market, listing_type=item.get("listing_type")
    )
    last_price = float(quality.get("last_price") or 0)
    if last_price <= 0:
        return {
            "error": f"Could not fetch a usable price for {symbol}: "
                     f"{quality.get('reason', 'no_price')}",
            "composite": composite,
            "grade": grade,
            "signals": signals,
        }

    plan = advisory_mod._entry_plan(
        last_price, side, atr_data.get("atr_pct"), item.get("currency", "USD"), cfg, grade,
    )

    try:
        ev = advisory_mod.compute_expected_value(
            composite, plan["suggested_size_eur"], [],
            getattr(regime_state, "intraday_regime", "ranging"),
            setup_context={"breakout_quality": breakout, "strategy_family": "advisory_manual",
                            "market": market},
            profile={"ev_breakout_probe_min_quality": 0.65},
        )
    except Exception:
        ev = {"net_ev_pct": None, "confidence": 0.0}

    # Decide stage like _scan_candidate does
    if (composite >= cfg.min_composite
            and grade in {"A+", "A"}
            and breakout >= cfg.min_breakout_quality
            and not late_chase):
        alert_stage = "trade"
    elif ignition and grade == "C":
        alert_stage = "ignition"
    elif ignition and not (composite >= cfg.min_watch_composite
                            and (breakout >= cfg.min_watch_breakout_quality or orb_active)):
        alert_stage = "ignition"
    else:
        alert_stage = "watch"

    now_cet = advisory_mod._now_cet()
    valid_until = now_cet.astimezone(timezone.utc) + timedelta(
        minutes=45 if alert_stage in advisory_mod.WATCH_ALERT_STAGES and market == "US"
                else (15 if market == "US" else 12)
    )
    time_exit = (now_cet.replace(hour=20, minute=55, second=0, microsecond=0)
                 if market == "US"
                 else now_cet.replace(hour=16, minute=45, second=0, microsecond=0))

    rationale = (
        f"{grade} setup, "
        f"VWAP {signals.get('vwap_deviation', {}).get('score', 0):+.2f}, "
        f"MACD {signals.get('macd_crossover', {}).get('score', 0):+.2f}, "
        f"RS {signals.get('relative_strength', {}).get('score', 0):+.2f}, "
        f"ORB {signals.get('orb', {}).get('score', 0):+.2f}"
    )

    record = {
        "market": market,
        "mode": "live",
        "status": "preview",
        "alert_stage": alert_stage,
        "data_symbol": symbol,
        "broker_display_name": item.get("broker_display_name") or symbol,
        "exchange": item.get("exchange"),
        "currency": item.get("currency", "USD"),
        "listing_type": item.get("listing_type"),
        "primary_symbol": item.get("primary_symbol"),
        "origin_market": item.get("origin_market"),
        "side": side,
        "grade": grade,
        "composite_score": round(composite, 4),
        "ev_net_pct": ev.get("net_ev_pct"),
        "breakout_quality": breakout,
        "confidence": ev.get("confidence", 0.0),
        "valid_until": valid_until.isoformat(),
        "time_exit_at": time_exit.astimezone(timezone.utc).isoformat(),
        "valid_until_cet": valid_until.astimezone(now_cet.tzinfo).strftime("%H:%M Berlin"),
        "time_exit_cet": time_exit.strftime("%H:%M Berlin"),
        "rationale": rationale,
        "late_chase_json": late_chase,
        "ignition_json": ignition,
        "pullback_confirmed": False,
        "fx_rate": cfg.fx_rate,
        "fx_rate_source": cfg.fx_rate_source,
        "fx_rate_fetched_at": cfg.fx_rate_fetched_at,
        **plan,
    }
    record["message_text"] = advisory_mod._format_trade_card(record)
    record["_signals_snapshot"] = signals
    return record


def _find_universe_item(advisory_mod, symbol: str) -> Optional[dict]:
    sym = symbol.upper()
    for market_items in advisory_mod.ADVISORY_UNIVERSE.values():
        for item in market_items:
            if item["data_symbol"].upper() == sym:
                return item
    return None


def _render_preview(result: dict, symbol: str):
    if "error" in result:
        st.error(result["error"])
        if "composite" in result:
            st.caption(
                f"Partial diagnostic — composite {result['composite']:.3f}, "
                f"grade {result.get('grade')}"
            )
        return

    stage = result.get("alert_stage", "watch")
    tone = STAGE_TONE.get(stage, "neutral")
    composite = float(result.get("composite_score") or 0)
    grade = result.get("grade", "?")
    side = result.get("side", "?")

    c1, c2, c3, c4 = st.columns(4)
    metric_card(c1, "Stage", STAGE_LABEL.get(stage, stage), tone=tone)
    metric_card(c2, "Grade", grade,
                tone="positive" if grade in ("A", "A+") else
                ("warning" if grade == "B" else "neutral"))
    metric_card(c3, "Composite", f"{composite:+.3f}",
                tone="positive" if composite > 0 else "negative")
    metric_card(c4, "Breakout", f"{result.get('breakout_quality', 0):.2f}", tone="info")

    ev_pct = result.get("ev_net_pct")
    flags = []
    if result.get("ignition_json"):
        flags.append("🔥 Momentum ignition detected")
    if result.get("late_chase_json"):
        d = result["late_chase_json"]
        flags.append(
            f"⚠️ Late-chase active — dev {d.get('pct_deviation')}% vs "
            f"threshold {d.get('threshold_pct')}%"
        )
    if not flags:
        flags.append("No special flags")
    st.markdown(" · ".join(flags))

    st.markdown("**Discord card preview**")
    st.code(result.get("message_text", ""), language="markdown")

    with st.expander("Raw signal scores", expanded=False):
        signals = result.pop("_signals_snapshot", {})
        if signals:
            st.json({
                k: {
                    "score": v.get("score") if isinstance(v, dict) else v,
                    **({"meta": v.get("meta")} if isinstance(v, dict) and v.get("meta") else {}),
                }
                for k, v in signals.items()
            })
        st.caption(f"Side: {side}  |  EV net: {ev_pct}%  |  FX: {result.get('fx_rate')}")
