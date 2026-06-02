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
    "downside": "negative",
}

STAGE_LABEL = {
    "trade": "🟢 TRADE",
    "watch": "🟡 WATCH",
    "ignition": "🔥 IGNITION",
    "downside": "🔻 DOWNSIDE",
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


def _query_param(name: str) -> Optional[str]:
    try:
        value = st.query_params.get(name)
        if isinstance(value, list):
            return str(value[0]) if value else None
        return str(value) if value not in (None, "") else None
    except Exception:
        return None


def _clear_query_params():
    try:
        st.query_params.clear()
    except Exception:
        pass


def _row_fx(row: dict) -> float:
    try:
        return float(row.get("fx_rate") or 1.0)
    except (TypeError, ValueError):
        return 1.0


def _native_to_eur(row: dict, value) -> Optional[float]:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    currency = str(row.get("currency") or "EUR").upper()
    if currency == "USD":
        return price / max(_row_fx(row), 0.0001)
    return price


def _eur_to_native(row: dict, value) -> Optional[float]:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    currency = str(row.get("currency") or "EUR").upper()
    if currency == "USD":
        return price * max(_row_fx(row), 0.0001)
    return price


def _entry_default_eur(row: dict) -> float:
    monitor = _as_dict(row.get("exit_monitor_json"))
    if monitor.get("manual_entry_price_eur"):
        return float(monitor["manual_entry_price_eur"])
    manual = _native_to_eur(row, row.get("manual_entry_price"))
    if manual:
        return manual
    entry_min = _native_to_eur(row, row.get("entry_min")) or 0.0
    entry_max = _native_to_eur(row, row.get("entry_max")) or entry_min
    return (entry_min + entry_max) / 2.0 if entry_min and entry_max else entry_min or entry_max


def _pnl_pct(row: dict, entry_eur: float, current_eur: float) -> float:
    if not entry_eur:
        return 0.0
    direction = 1 if str(row.get("side") or "BUY").upper() == "BUY" else -1
    return ((current_eur - entry_eur) / entry_eur) * 100.0 * direction


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


# Gate reason descriptions for the legend
_GATE_REASON_LABELS = {
    "no_candidate":          "Signal below threshold or composite ≤ 0 (no downside path either)",
    "already_alerted_session": "Symbol already alerted this session — duplicate suppressed",
    "watch_repeat_blocked":  "Watch alert recently sent for this symbol — repeat suppressed",
    "benchmark_only":        "Benchmark ticker (SPY/QQQ) — logged for context, no trade card",
    "blocked_data_quality":  "Data quality gate failed (stale bars, too few bars, etc.)",
    "blocked_filter":        "FX rate unavailable — EUR display blocked",
    "alerted":               "Alert successfully sent to Discord",
    "emit_failed":           "Emit failed (DB write error or discord send issue)",
    "capped_by_limit":       "Queued but not sent — daily/session alert cap reached",
}


_GRADE_RANK = {"A+": 0, "A": 1, "B": 2, "C": 3}


def _render_live_scan_log(fetch_fn):
    """Render historical scan decisions with per-ticker gate reasons."""
    st.subheader("Debug: Scan History")
    st.caption(
        "Historical ticker-level scan decisions with gate reasons. "
        "Green = alerted to Discord. Red = downside risk. Grey = blocked."
    )
    if fetch_fn is None:
        st.info("Live scan log is not available in this deployment yet.")
        return

    c_mkt, c_hrs, c_refresh = st.columns([2, 2, 2])
    with c_mkt:
        scan_market = st.selectbox("Market", ["US", "EU"], index=0, key="scan_log_market")
    with c_hrs:
        scan_hours = st.selectbox("Hours back", [1, 2, 4, 8], index=0, key="scan_log_hours")
    with c_refresh:
        st.write("")
        st.write("")
        refresh_clicked = st.button("Refresh", key="scan_log_refresh")

    try:
        scan_rows = fetch_fn(market=scan_market, hours_back=scan_hours, limit=300)
    except Exception as exc:
        st.warning(f"Could not load scan log: {exc}")
        return

    if refresh_clicked:
        st.rerun()

    if not scan_rows:
        st.info(
            f"No scan log entries for {scan_market} in the last {scan_hours}h. "
            "Scan log is written during advisory cycles."
        )
        return

    st.caption(f"Last refresh: {_format_time(scan_rows[0].get('scanned_at'))} · {len(scan_rows)} rows")

    table_rows = []
    for r in scan_rows:
        grade = r.get("grade") or "—"
        table_rows.append({
            "_grade_rank": _GRADE_RANK.get(grade, 9),
            "_alerted": bool(r.get("alerted")),
            "Time": _format_time(r.get("scanned_at")),
            "Symbol": r.get("data_symbol") or "—",
            "Window": r.get("session_window") or "—",
            "Grade": grade,
            "Side": r.get("side") or "—",
            "Composite": float(r.get("composite_score") or 0),
            "Breakout": float(r.get("breakout_quality") or 0) if r.get("breakout_quality") is not None else None,
            "EV%": float(r.get("ev_net_pct") or 0) if r.get("ev_net_pct") is not None else None,
            "VWAP": float(r.get("vwap_score") or 0) if r.get("vwap_score") is not None else None,
            "MACD": float(r.get("macd_score") or 0) if r.get("macd_score") is not None else None,
            "RS": float(r.get("rel_strength_score") or 0) if r.get("rel_strength_score") is not None else None,
            "Tape": float(r.get("tape_score") or 0) if r.get("tape_score") is not None else None,
            "RSI": float(r.get("rsi_score") or 0) if r.get("rsi_score") is not None else None,
            "ORB": bool(r.get("orb_active")) if r.get("orb_active") is not None else None,
            "Gate": r.get("gate_reason") or "—",
            "Alerted": bool(r.get("alerted")),
            "Downside": bool(r.get("downside_risk")),
        })

    df = pd.DataFrame(table_rows)
    # Sort: alerted first, then by grade rank (A+→A→B→C→ungraded), then composite desc
    df = df.sort_values(
        ["_alerted", "_grade_rank", "Composite"],
        ascending=[False, True, False],
    ).drop(columns=["_grade_rank", "_alerted"])

    def _row_color(row):
        if row.get("Alerted"):
            return ["background-color: #14532d22"] * len(row)
        if row.get("Downside"):
            return ["background-color: #7f1d1d22"] * len(row)
        gate = str(row.get("Gate") or "")
        if "blocked" in gate or gate in {"no_candidate", "capped_by_limit", "watch_repeat_blocked", "already_alerted_session"}:
            return ["background-color: #1f1f1f"] * len(row)
        return [""] * len(row)

    try:
        styled = df.style.apply(_row_color, axis=1)
        st.dataframe(styled, hide_index=True, use_container_width=True)
    except Exception:
        st.dataframe(df, hide_index=True, use_container_width=True)

    with st.expander("Gate Reason Legend", expanded=False):
        for reason, desc in _GATE_REASON_LABELS.items():
            st.markdown(f"- **`{reason}`**: {desc}")


def render():
    from database import client as db_client

    def _missing_row(*args, **kwargs):
        return {}

    def _missing_rows(*args, **kwargs):
        return []

    def _missing_update(*args, **kwargs):
        return {"error": "This advisory database helper is not available in the deployed app version."}

    get_advisory_signal_by_id = getattr(db_client, "get_advisory_signal_by_id", _missing_row)
    get_open_advisory_positions = getattr(db_client, "get_open_advisory_positions", _missing_rows)
    get_recent_advisory_signals = getattr(db_client, "get_recent_advisory_signals", _missing_rows)
    mark_advisory_taken = getattr(db_client, "mark_advisory_taken", _missing_update)
    update_advisory_exit_status = getattr(db_client, "update_advisory_exit_status", _missing_update)
    record_advisory_manual_trade = getattr(db_client, "record_advisory_manual_trade", _missing_update)
    get_advisory_trades = getattr(db_client, "get_advisory_trades", _missing_rows)
    _get_advisory_scan_log = getattr(db_client, "get_advisory_scan_log", None)
    _get_advisory_scoreboard = getattr(db_client, "get_advisory_scoreboard", None)
    _get_latest_scan_snapshots = getattr(db_client, "get_latest_advisory_scan_snapshots", None)
    _get_advisory_attribution_summary = getattr(db_client, "get_advisory_attribution_summary", None)
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

    _render_mark_taken_banner(get_advisory_signal_by_id, mark_advisory_taken)
    _render_open_positions(get_open_advisory_positions, record_advisory_manual_trade)
    _render_closed_advisory_trades(get_advisory_trades)
    if _get_advisory_attribution_summary is not None:
        _render_attribution_summary(_get_advisory_attribution_summary)

    st.divider()

    if _get_latest_scan_snapshots is not None:
        _render_live_scan_table(_get_latest_scan_snapshots)

    st.divider()

    # ── Replay Scoreboard ──────────────────────────────────────────────────
    if _get_advisory_scoreboard is not None:
        _render_scoreboard(_get_advisory_scoreboard)

    st.divider()

    # ── Historical scan debug log ──────────────────────────────────────────
    with st.expander("Debug: Scan History", expanded=False):
        _render_live_scan_log(_get_advisory_scan_log)

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
    mode_options  = ["all", "live", "shadow"]

    c_stage, c_grade, c_mode, c_symbol = st.columns([2, 2, 2, 3])
    with c_stage:
        stage_filter = st.radio("Stage", stage_options, index=0, horizontal=True,
                                key="advisory_stage_filter")
    with c_grade:
        grade_filter = st.multiselect("Grade", ["A+", "A", "B", "C"],
                                      default=[], placeholder="All grades",
                                      key="advisory_grade_filter")
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
        if grade_filter and str(r.get("grade") or "").upper() not in grade_filter:
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


# ── Manual Entry / Open Position Tracking ───────────────────────────────────

def _render_mark_taken_banner(fetch_signal_fn, mark_taken_fn):
    mark_id = _query_param("mark_id")
    if not mark_id:
        return
    try:
        signal_id = int(mark_id)
    except (TypeError, ValueError):
        st.warning("The mark-as-taken link is invalid.")
        return

    row = fetch_signal_fn(signal_id) or {}
    if not row:
        st.warning(f"No advisory signal found for mark_id={signal_id}.")
        return

    entry_default = _entry_default_eur(row)
    size_default = float(row.get("suggested_size_eur") or 0)
    entry_min_eur = _native_to_eur(row, row.get("entry_min"))
    entry_max_eur = _native_to_eur(row, row.get("entry_max"))
    stop_eur = _native_to_eur(row, row.get("stop_price"))
    t1_eur = _native_to_eur(row, row.get("target_1"))
    t2_eur = _native_to_eur(row, row.get("target_2"))

    with st.container(border=True):
        st.markdown(
            f"**Mark as taken: {row.get('data_symbol')} {row.get('side')} "
            f"· grade {row.get('grade')} · {_format_time(row.get('created_at'))}**"
        )
        st.caption(
            f"Suggested band €{entry_min_eur or 0:.2f}-€{entry_max_eur or 0:.2f} · "
            f"stop €{stop_eur or 0:.2f} · T1 €{t1_eur or 0:.2f} · T2 €{t2_eur or 0:.2f}"
        )
        with st.form(f"mark_taken_{signal_id}"):
            c_entry, c_size = st.columns(2)
            with c_entry:
                entry_price_eur = st.number_input(
                    "Your entry price (€)",
                    min_value=0.0,
                    value=float(entry_default or 0.0),
                    step=0.01,
                    format="%.2f",
                )
            with c_size:
                size_eur = st.number_input(
                    "Position size (€)",
                    min_value=0.0,
                    value=float(size_default or 0.0),
                    step=50.0,
                    format="%.2f",
                )
            notes = st.text_input("Notes", value="", placeholder="Optional: why you took it")
            submitted = st.form_submit_button("Confirm entry")

        c_clear, _ = st.columns([1, 5])
        with c_clear:
            if st.button("Dismiss", key=f"dismiss_mark_{signal_id}"):
                _clear_query_params()
                st.rerun()

        if submitted:
            result = mark_taken_fn(
                signal_id,
                entry_price_eur=float(entry_price_eur),
                size_eur=float(size_eur),
                notes=notes or None,
            )
            if result.get("error"):
                st.error(f"Could not mark advisory as taken: {result['error']}")
            else:
                st.success("Entry recorded. Exit monitoring will use this price.")
                _clear_query_params()
                st.rerun()


def _render_open_positions(fetch_open_fn, record_exit_fn):
    try:
        positions = fetch_open_fn(max_age_days=7) or []
    except Exception as exc:
        st.warning(f"Open advisory positions unavailable: {exc}")
        return
    if not positions:
        return

    st.subheader("Open advisory positions")
    rows = []
    for pos in positions:
        monitor = _as_dict(pos.get("exit_monitor_json"))
        entry_eur = _entry_default_eur(pos)
        current_eur = monitor.get("last_price_eur")
        if current_eur is None and monitor.get("last_price_native") is not None:
            current_eur = _native_to_eur(pos, monitor.get("last_price_native"))
        current_eur = float(current_eur) if current_eur is not None else None
        pnl_pct = _pnl_pct(pos, entry_eur, current_eur) if current_eur is not None else None
        size_eur = float(monitor.get("size_eur") or pos.get("suggested_size_eur") or 0)
        rows.append({
            "symbol": pos.get("data_symbol"),
            "side": pos.get("side"),
            "grade": pos.get("grade"),
            "entry": f"€{entry_eur:.2f}" if entry_eur else "—",
            "last": f"€{current_eur:.2f}" if current_eur is not None else "pending",
            "pnl": f"{pnl_pct:+.2f}%" if pnl_pct is not None else "—",
            "size": f"€{size_eur:.0f}" if size_eur else "—",
            "alerts": ", ".join(monitor.get("alerts") or []) or "—",
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    for pos in positions:
        signal_id = int(pos.get("id"))
        monitor = _as_dict(pos.get("exit_monitor_json"))
        entry_eur = _entry_default_eur(pos)
        current_eur = monitor.get("last_price_eur")
        if current_eur is None and monitor.get("last_price_native") is not None:
            current_eur = _native_to_eur(pos, monitor.get("last_price_native"))
        current_eur = float(current_eur) if current_eur is not None else float(entry_eur or 0)

        with st.expander(f"Close {pos.get('data_symbol')} {pos.get('side')} from advisory #{signal_id}"):
            with st.form(f"close_advisory_{signal_id}"):
                c_exit, c_notes = st.columns([1, 2])
                with c_exit:
                    exit_price_eur = st.number_input(
                        "Exit price (€)",
                        min_value=0.0,
                        value=float(current_eur or entry_eur or 0.0),
                        step=0.01,
                        format="%.2f",
                        key=f"exit_price_{signal_id}",
                    )
                with c_notes:
                    notes = st.text_input("Exit notes", key=f"exit_notes_{signal_id}")
                close_submitted = st.form_submit_button("Record exit")

            if close_submitted:
                result = record_exit_fn(signal_id, exit_price_eur, notes=notes or "")
                if result.get("error"):
                    st.error(f"Could not record exit: {result['error']}")
                else:
                    pnl = result.get("pnl_eur", 0)
                    st.success(f"Exit recorded and saved to trade history. P&L: €{float(pnl):+.2f}")
                    st.rerun()


# ── Advisory Attribution Summary ─────────────────────────────────────────────

def _render_attribution_summary(summary_fn):
    """Attribution panel: manual trade P&L vs signal quality (last 90 days)."""
    try:
        summary = summary_fn(days=90)
    except Exception as exc:
        st.caption(f"Attribution summary unavailable: {exc}")
        return

    if not summary or summary.get("total_trades", 0) == 0:
        return

    with st.expander(
        f"Advisory attribution (90d) — {summary['total_trades']} trades "
        f"€{summary['total_pnl_eur']:+.2f} P&L",
        expanded=False,
    ):
        mc1, mc2, mc3, mc4 = st.columns(4)
        mc1.metric("Linked trades", summary["total_trades"])
        mc2.metric("Total P&L", f"€{summary['total_pnl_eur']:+.2f}")
        mc3.metric("Win rate", f"{summary['win_rate']:.0f}%")
        mc4.metric("Avg P&L", f"{summary['avg_pnl_pct']:+.2f}%")

        if summary.get("by_grade"):
            st.caption("By grade")
            grade_rows = [
                {
                    "Grade": r["grade"], "Trades": r["count"],
                    "Wins": r["wins"], "Win %": f"{r['win_rate']:.0f}%",
                    "Avg P&L": f"{r['avg_pnl_pct']:+.2f}%",
                    "Total €": f"€{r['total_pnl_eur']:+.2f}",
                }
                for r in summary["by_grade"]
            ]
            st.dataframe(pd.DataFrame(grade_rows), hide_index=True, use_container_width=True)

        if summary.get("by_ticker"):
            st.caption("By ticker")
            ticker_rows = [
                {
                    "Ticker": r["ticker"], "Trades": r["count"],
                    "Win %": f"{r['win_rate']:.0f}%",
                    "Avg P&L": f"{r['avg_pnl_pct']:+.2f}%",
                    "Total €": f"€{r['total_pnl_eur']:+.2f}",
                    "Grades": ", ".join(r.get("grades") or []),
                }
                for r in summary["by_ticker"]
            ]
            st.dataframe(pd.DataFrame(ticker_rows), hide_index=True, use_container_width=True)

        if summary.get("signal_vs_execution"):
            st.caption("Signal vs execution (where 60m forward return is available)")
            sve_rows = [
                {
                    "Ticker": r["ticker"], "Grade": r["grade"],
                    "Manual P&L": f"{r['manual_pnl_pct']:+.2f}%",
                    "Fwd 60m": f"{r['forward_return_60m']:+.2f}%",
                    "Outperformed": "yes" if r["outperformed"] else "no",
                }
                for r in summary["signal_vs_execution"]
            ]
            st.dataframe(pd.DataFrame(sve_rows), hide_index=True, use_container_width=True)

        if summary.get("missed_winners"):
            st.caption(
                f"Missed winners — {len(summary['missed_winners'])} signals not taken "
                f"with >1% 60m forward return"
            )
            mw_rows = [
                {
                    "Date": str(r.get("created_at") or "")[:10],
                    "Ticker": r["ticker"], "Grade": r["grade"],
                    "Fwd 60m": f"{r['forward_return_60m']:+.2f}%",
                    "Score": f"{r.get('composite_score', 0):.2f}",
                }
                for r in summary["missed_winners"]
            ]
            st.dataframe(pd.DataFrame(mw_rows), hide_index=True, use_container_width=True)


# ── Closed Advisory Trades ───────────────────────────────────────────────────

def _render_closed_advisory_trades(fetch_fn):
    try:
        trades = fetch_fn(days=90) or []
    except Exception as exc:
        st.warning(f"Advisory trade history unavailable: {exc}")
        return

    if not trades:
        return

    st.subheader("Closed advisory trades")
    st.caption("Trades executed from advisory signals via Trade Republic or the close form.")

    df = pd.DataFrame(trades)
    df["pnl_eur"]     = pd.to_numeric(df.get("pnl_eur", 0), errors="coerce").fillna(0)
    df["net_pnl_pct"] = pd.to_numeric(df.get("net_pnl_pct", 0), errors="coerce").fillna(0)

    total_pnl  = df["pnl_eur"].sum()
    win_count  = int((df["net_pnl_pct"] > 0).sum())
    win_rate   = win_count / len(df) * 100 if len(df) else 0
    avg_pnl    = df["net_pnl_pct"].mean() if len(df) else 0

    mc1, mc2, mc3, mc4 = st.columns(4)
    mc1.metric("Trades", len(df))
    mc2.metric("Total P&L", f"€{total_pnl:+.2f}")
    mc3.metric("Win rate", f"{win_rate:.0f}%")
    mc4.metric("Avg P&L", f"{avg_pnl:+.2f}%")

    # Join advisory grade when advisory_signal_id is present
    grade_map: dict = {}
    signal_ids = [r.get("advisory_signal_id") for r in trades if r.get("advisory_signal_id")]
    if signal_ids:
        try:
            from database.client import get_client as _gc
            res = (_gc().table("advisory_signals")
                        .select("id,grade,alert_stage,data_symbol")
                        .in_("id", signal_ids)
                        .execute())
            grade_map = {row["id"]: row for row in (res.data or [])}
        except Exception:
            pass

    display_rows = []
    for t in trades:
        sig = grade_map.get(t.get("advisory_signal_id")) or {}
        display_rows.append({
            "Date": (t.get("exit_time") or t.get("created_at") or "")[:10],
            "Ticker": t.get("ticker"),
            "Side": t.get("side"),
            "Grade": sig.get("grade") or "—",
            "Entry €": f"€{float(t['entry_price']):.2f}" if t.get("entry_price") else "—",
            "Exit €": f"€{float(t['exit_price']):.2f}" if t.get("exit_price") else "—",
            "P&L €": f"€{float(t['pnl_eur']):+.2f}" if t.get("pnl_eur") is not None else "—",
            "P&L %": f"{float(t['net_pnl_pct']):+.2f}%" if t.get("net_pnl_pct") is not None else "—",
        })

    display_df = pd.DataFrame(display_rows)

    def _highlight(val):
        if isinstance(val, str) and val.startswith("€"):
            try:
                num = float(val.replace("€", "").replace(",", ""))
                if num > 0:
                    return "color: #00d4a0"
                if num < 0:
                    return "color: #ff5c5c"
            except ValueError:
                pass
        return ""

    st.dataframe(
        display_df.style.map(_highlight, subset=["P&L €", "P&L %"] if "P&L €" in display_df.columns else []),
        hide_index=True,
        use_container_width=True,
    )


# ── Live Scan Table ─────────────────────────────────────────────────────────

def _render_live_scan_table(fetch_fn):
    st.subheader("Latest Scan")
    st.caption("Current per-ticker advisory state from the latest scan cycle, including non-alert gate reasons.")

    c_market, c_limit, _ = st.columns([2, 2, 6])
    with c_market:
        market = st.radio("Scan market", ["US", "EU", "all"], index=0, horizontal=True,
                          key="advisory_scan_market")
    with c_limit:
        limit = st.selectbox("Rows", [25, 50, 100], index=1, key="advisory_scan_limit")

    rows = fetch_fn(market=market, limit=limit) or []
    if not rows:
        st.info("No scan snapshots yet. Run one advisory cycle after applying the scan snapshot migration.")
        return

    latest_cycle = rows[0].get("cycle_id")
    latest_rows = [r for r in rows if r.get("cycle_id") == latest_cycle] or rows
    table = pd.DataFrame([
        {
            "updated": _format_time(r.get("cycle_started_at") or r.get("created_at")),
            "window": r.get("window"),
            "symbol": r.get("data_symbol"),
            "primary": r.get("primary_symbol") or r.get("data_symbol"),
            "name": r.get("broker_display_name"),
            "stage": STAGE_LABEL.get(str(r.get("alert_stage") or ""), r.get("alert_stage") or "—"),
            "side": r.get("side") or "—",
            "grade": r.get("grade") or "—",
            "composite": float(r.get("composite_score") or 0),
            "ev_pct": float(r.get("ev_net_pct") or 0) if r.get("ev_net_pct") is not None else None,
            "breakout": float(r.get("breakout_quality") or 0) if r.get("breakout_quality") is not None else None,
            "price": float(r.get("last_price") or 0) if r.get("last_price") is not None else None,
            "gate": r.get("gate_reason") or r.get("status"),
        }
        for r in latest_rows
    ])
    if "composite" in table:
        table = table.assign(abs_composite=table["composite"].abs()).sort_values(
            ["abs_composite", "breakout"], ascending=[False, False]
        ).drop(columns=["abs_composite"])
    st.dataframe(
        table,
        width="stretch",
        hide_index=True,
        column_config=column_config(table.columns),
    )


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


def _render_scoreboard(fetch_fn):
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

    sb_rows = fetch_fn(
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

    df["alert_stage"] = df.apply(lambda row: _stage_of(row.to_dict()), axis=1)
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
            f"{mfe.mean():+.2f}% / {mae.mean():+.2f}%"
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
            "avg MFE/MAE": mfe_mae_str,
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
                     win_pct=lambda x: float((x.dropna() > 0).mean() * 100) if len(x.dropna()) else float("nan"))
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
