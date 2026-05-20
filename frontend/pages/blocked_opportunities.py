"""frontend/pages/blocked_opportunities.py - Blocked opportunity observability."""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, time, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8 fallback for older local/dev runtimes.
    ZoneInfo = None

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from frontend.ui_help import column_config
from frontend.ui_theme import (
    ACCENT,
    INFO,
    WARNING,
    apply_plotly_theme,
    metric_card,
    modern_section,
    page_header,
    status_pill,
)


STAGE_ORDER = [
    "signal_consensus",
    "signal_alignment",
    "reward_risk",
    "regime",
    "ev",
    "ranking",
    "gate",
    "conviction",
    "llm",
    "sizing",
    "exposure",
    "position",
    "time",
    "price",
]

SIGNAL_KEYS = [
    "composite_score",
    "rsi_divergence",
    "vwap_deviation",
    "news_sentiment",
    "tape_aggression",
    "order_book_imbalance",
    "macd_crossover",
    "relative_strength",
    "bollinger_squeeze",
    "put_call_ratio",
    "orb",
]


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


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _safe_float(value, default=0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_ts(value):
    if not value:
        return pd.NaT
    return pd.to_datetime(value, utc=True, errors="coerce")


def _local_tz():
    if ZoneInfo is None:
        return timezone.utc
    return ZoneInfo(os.getenv("DASHBOARD_TIMEZONE", "Europe/Berlin"))


def _today_local_bounds() -> tuple[pd.Timestamp, pd.Timestamp]:
    tz = _local_tz()
    today = datetime.now(tz).date()
    start = pd.Timestamp(datetime.combine(today, time.min), tz=tz).tz_convert("UTC")
    end = pd.Timestamp(datetime.combine(today, time.max), tz=tz).tz_convert("UTC")
    return start, end


def _stage_sort_key(stage: str) -> tuple[int, str]:
    value = str(stage or "unknown")
    try:
        return STAGE_ORDER.index(value), value
    except ValueError:
        return len(STAGE_ORDER), value


def _runner_severity(row: pd.Series) -> str:
    replay = _as_dict(row.get("replay_result_json"))
    severity = replay.get("runner_severity")
    if severity:
        return str(severity)
    favorable = _safe_float(row.get("max_favorable_pct"))
    close_after = _safe_float(row.get("close_after_pct"))
    if favorable >= 2.0 and close_after > 0:
        return "runner"
    if favorable >= 0.75 and close_after > 0:
        return "minor"
    if row.get("replay_checked_at") and (close_after <= 0 or _safe_float(row.get("max_adverse_pct")) <= -0.5):
        return "avoided"
    return ""


def _threshold_gap(row: pd.Series):
    detail = _as_dict(row.get("block_detail"))
    return detail.get("threshold_gap")


def _near_threshold(row: pd.Series) -> bool:
    detail = _as_dict(row.get("block_detail"))
    return bool(detail.get("near_threshold"))


def _detail_value(row: pd.Series, key: str):
    return _as_dict(row.get("block_detail")).get(key)


def _signal_score(value):
    if isinstance(value, dict):
        if "score" in value:
            return value.get("score")
        return None
    return value


def _flatten_signals(row: pd.Series) -> dict:
    signals = _as_dict(row.get("signals_json"))
    out = {"composite_score": row.get("composite_score")}
    for key in SIGNAL_KEYS:
        if key == "composite_score":
            continue
        out[key] = _signal_score(signals.get(key))
    return out


def _display_time(series: pd.Series) -> pd.Series:
    tz = _local_tz()
    parsed = pd.to_datetime(series, utc=True, errors="coerce")
    return parsed.dt.tz_convert(tz).dt.strftime("%Y-%m-%d %H:%M")


def _style_severity(df: pd.DataFrame):
    def row_style(row):
        severity = row.get("runner_severity", "")
        if severity == "runner":
            return ["background-color: rgba(0, 212, 160, 0.18)"] * len(row)
        if severity == "minor":
            return ["background-color: rgba(255, 193, 7, 0.14)"] * len(row)
        if severity == "avoided":
            return ["background-color: rgba(255, 92, 92, 0.10)"] * len(row)
        return [""] * len(row)

    return df.style.apply(row_style, axis=1)


def _dataframe(df: pd.DataFrame, *, styled: bool = False):
    if df.empty:
        st.info("No rows for the current filters.")
        return
    st.dataframe(
        _style_severity(df) if styled else df,
        use_container_width=True,
        hide_index=True,
        column_config=column_config(df.columns),
    )


def _load_data(days: int, limit: int) -> pd.DataFrame:
    from database.client import get_blocked_opportunities

    rows = get_blocked_opportunities(days=days, limit=limit)
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    for col in [
        "composite_score",
        "candidate_rank_score",
        "breakout_quality",
        "ev_net_pct",
        "reference_price",
        "max_favorable_pct",
        "max_adverse_pct",
        "close_after_pct",
        "minutes_since_open",
        "atr_pct",
        "spread_pct",
        "opening_range_position",
    ]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["created_at_ts"] = df["created_at"].map(_parse_ts)
    df["created_local"] = _display_time(df["created_at"])
    df["runner_severity"] = df.apply(_runner_severity, axis=1)
    df["threshold_gap"] = df.apply(_threshold_gap, axis=1)
    df["near_threshold"] = df.apply(_near_threshold, axis=1)
    df["threshold_score"] = df.apply(lambda row: _detail_value(row, "score"), axis=1)
    df["threshold"] = df.apply(lambda row: _detail_value(row, "threshold"), axis=1)
    return df


def _filter_df(df: pd.DataFrame, tickers: list[str], stages: list[str], severities: list[str]) -> pd.DataFrame:
    out = df.copy()
    if tickers:
        out = out[out["ticker"].isin(tickers)]
    if stages:
        out = out[out["block_stage"].isin(stages)]
    if severities:
        out = out[out["runner_severity"].isin(severities)]
    return out


def _render_stage_breakdown(df: pd.DataFrame):
    modern_section("Blocks By Gate Stage", "Where candidates stopped before becoming trades.")
    start, end = _today_local_bounds()
    today = df[(df["created_at_ts"] >= start) & (df["created_at_ts"] <= end)].copy()
    count_label = "Today blocked"
    if today.empty:
        latest_day = df["created_at_ts"].dt.date.max()
        today = df[df["created_at_ts"].dt.date == latest_day].copy()
        if today.empty:
            st.info("No blocked opportunities recorded today.")
            return
        count_label = "Day blocked"
        st.caption(f"No blocked-opportunity rows for the current local day; showing latest stored block day: {latest_day}.")

    counts = Counter(today["block_stage"].fillna("unknown"))
    ordered = sorted(counts.items(), key=lambda item: _stage_sort_key(item[0]))
    breakdown = "/".join(str(count) for _, count in ordered)

    c1, c2, c3, c4 = st.columns(4)
    metric_card(c1, count_label, f"{len(today):,}", tone="info")
    metric_card(c2, "Stage breakdown", breakdown)
    metric_card(c3, "Replayed", f"{today['replay_checked_at'].notna().sum():,}")
    metric_card(c4, "Runner blocks", f"{(today['runner_severity'] == 'runner').sum():,}", tone="warning")

    stage_df = pd.DataFrame(
        [
            {
                "stage": stage,
                "blocks": count,
                "share_pct": round(count / len(today) * 100, 1),
                "replayed": int(today[today["block_stage"] == stage]["replay_checked_at"].notna().sum()),
                "runners": int((today[today["block_stage"] == stage]["runner_severity"] == "runner").sum()),
            }
            for stage, count in ordered
        ]
    )

    col_chart, col_table = st.columns([1.2, 1])
    with col_chart:
        fig = go.Figure(go.Bar(
            x=stage_df["blocks"],
            y=stage_df["stage"],
            orientation="h",
            text=stage_df["blocks"].map(lambda v: f"{v:,}"),
            marker_color=ACCENT,
        ))
        apply_plotly_theme(fig, height=max(280, 28 * len(stage_df)))
        fig.update_yaxes(autorange="reversed")
        st.plotly_chart(fig, use_container_width=True)
    with col_table:
        _dataframe(stage_df)


def _render_eod_gate_activity():
    try:
        from database.client import get_daily_reviews

        reviews = get_daily_reviews(limit=5)
    except Exception:
        reviews = []
    if not reviews:
        return

    latest = reviews[0]
    metrics = _as_dict(latest.get("metrics_json"))
    gate = _as_dict(metrics.get("gate_activity"))
    event_counts = _as_dict(gate.get("event_counts"))
    if not event_counts:
        return

    modern_section("Latest EOD Gate Activity", "Log-derived gate counts from the latest daily review.")
    ordered = sorted(event_counts.items(), key=lambda item: item[1], reverse=True)
    review_date = latest.get("review_date") or "latest review"
    breakdown = "/".join(str(count) for _, count in ordered)

    c1, c2, c3 = st.columns(3)
    metric_card(c1, "Review date", review_date)
    metric_card(c2, "Gate events", f"{sum(int(v or 0) for _, v in ordered):,}", tone="warning")
    metric_card(c3, "Event breakdown", breakdown)

    col_chart, col_table = st.columns([1.2, 1])
    with col_chart:
        fig = go.Figure(go.Bar(
            x=[item[1] for item in ordered],
            y=[item[0] for item in ordered],
            orientation="h",
            text=[f"{item[1]:,}" for item in ordered],
            marker_color=WARNING,
        ))
        apply_plotly_theme(fig, height=max(260, 30 * len(ordered)))
        fig.update_yaxes(autorange="reversed")
        st.plotly_chart(fig, use_container_width=True)
    with col_table:
        event_df = pd.DataFrame([{"event": k, "count": v} for k, v in ordered])
        _dataframe(event_df)

    top_veto = _as_list(gate.get("top_veto_tickers"))
    if top_veto:
        modern_section("Most Vetoed In Latest Review")
        _dataframe(pd.DataFrame(top_veto).head(12))


def _render_missed_runners(df: pd.DataFrame):
    modern_section("Missed Runners", "Blocked candidates whose replay later moved favorably.")
    checked = df[df["replay_checked_at"].notna()].copy()
    runners = checked[
        (checked["runner_severity"].isin(["runner", "minor"]))
        | ((checked["max_favorable_pct"].fillna(0) >= 0.75) & (checked["close_after_pct"].fillna(0) > 0))
    ].sort_values(["runner_severity", "max_favorable_pct"], ascending=[False, False])

    cols = [
        "id",
        "created_local",
        "ticker",
        "action_hint",
        "block_stage",
        "block_reason",
        "runner_severity",
        "max_favorable_pct",
        "max_adverse_pct",
        "close_after_pct",
        "threshold_gap",
        "setup_grade",
        "candidate_rank_score",
    ]
    _dataframe(runners[[c for c in cols if c in runners.columns]].head(50), styled=True)


def _render_near_threshold(df: pd.DataFrame):
    modern_section("Near-Threshold Distribution", "Candidates that landed close to a configured gate threshold.")
    near = df[df["near_threshold"]].copy()
    if near.empty:
        st.info("No near-threshold blocks recorded in the current window.")
        return

    checked = near[near["replay_checked_at"].notna()]
    c1, c2, c3, c4 = st.columns(4)
    metric_card(c1, "Near threshold", f"{len(near):,}", tone="info")
    metric_card(c2, "Checked", f"{len(checked):,}")
    metric_card(c3, "Runners", f"{(near['runner_severity'] == 'runner').sum():,}", tone="warning")
    metric_card(c4, "Median gap", f"{checked['threshold_gap'].median():.4f}" if len(checked) else "—")

    bins = pd.cut(
        pd.to_numeric(near["threshold_gap"], errors="coerce"),
        bins=[-0.001, 0.0025, 0.005, 0.01, 0.02, 1],
        labels=["<=0.25%", "<=0.50%", "<=1.00%", "<=2.00%", ">2.00%"],
    )
    dist = (
        near.assign(gap_bucket=bins)
        .groupby(["gap_bucket", "runner_severity"], dropna=False, observed=False)
        .size()
        .reset_index(name="blocks")
    )
    if not dist.empty:
        fig = go.Figure()
        for severity in ["runner", "minor", "avoided", ""]:
            subset = dist[dist["runner_severity"].fillna("") == severity]
            if subset.empty:
                continue
            fig.add_trace(go.Bar(
                x=subset["gap_bucket"].astype(str),
                y=subset["blocks"],
                name=severity or "unchecked/neutral",
            ))
        apply_plotly_theme(fig, height=270)
        fig.update_layout(barmode="stack")
        st.plotly_chart(fig, use_container_width=True)

    cols = [
        "created_local",
        "ticker",
        "action_hint",
        "block_stage",
        "block_reason",
        "runner_severity",
        "threshold_score",
        "threshold",
        "threshold_gap",
        "max_favorable_pct",
        "max_adverse_pct",
        "close_after_pct",
    ]
    table = near.sort_values(["runner_severity", "max_favorable_pct"], ascending=[False, False])
    _dataframe(table[[c for c in cols if c in table.columns]].head(75), styled=True)


def _render_direction_errors(df: pd.DataFrame):
    modern_section("Direction Error Candidates", "Blocked setups whose replay moved strongly against the hinted direction.")
    checked = df[df["replay_checked_at"].notna()].copy()
    candidates = checked[
        (checked["max_adverse_pct"].fillna(0) <= -2.0)
        | (checked["close_after_pct"].fillna(0) <= -1.0)
    ].sort_values(["close_after_pct", "max_adverse_pct"], ascending=True)
    if candidates.empty:
        st.info("No direction-error candidates in the current window.")
        return

    rows = []
    for _, row in candidates.head(30).iterrows():
        base = {
            "created_local": row.get("created_local"),
            "ticker": row.get("ticker"),
            "action_hint": row.get("action_hint"),
            "block_stage": row.get("block_stage"),
            "block_reason": row.get("block_reason"),
            "max_favorable_pct": row.get("max_favorable_pct"),
            "max_adverse_pct": row.get("max_adverse_pct"),
            "close_after_pct": row.get("close_after_pct"),
        }
        base.update(_flatten_signals(row))
        rows.append(base)
    table = pd.DataFrame(rows)
    _dataframe(table, styled=False)


def _render_most_vetoed(df: pd.DataFrame):
    modern_section("Tickers Most Vetoed", "Symbols repeatedly stopped by gates, useful for unfair-block checks.")
    if df.empty:
        st.info("No veto rows in the current window.")
        return

    grouped = (
        df.groupby("ticker", dropna=False)
        .agg(
            blocks=("id", "count"),
            stages=("block_stage", lambda s: ", ".join(f"{k}:{v}" for k, v in Counter(s).most_common(3))),
            top_reason=("block_reason", lambda s: Counter(s.fillna("unknown")).most_common(1)[0][0]),
            replayed=("replay_checked_at", lambda s: int(s.notna().sum())),
            runners=("runner_severity", lambda s: int((s == "runner").sum())),
            avg_mfe=("max_favorable_pct", "mean"),
            avg_close=("close_after_pct", "mean"),
        )
        .reset_index()
        .sort_values(["blocks", "runners"], ascending=False)
    )

    col_chart, col_table = st.columns([1, 1.35])
    with col_chart:
        top = grouped.head(12)
        fig = go.Figure(go.Bar(
            x=top["blocks"],
            y=top["ticker"],
            orientation="h",
            text=top["blocks"].map(lambda v: f"{v:,}"),
            marker_color=INFO,
        ))
        apply_plotly_theme(fig, height=320)
        fig.update_yaxes(autorange="reversed")
        st.plotly_chart(fig, use_container_width=True)
    with col_table:
        display = grouped.head(30).copy()
        for col in ["avg_mfe", "avg_close"]:
            display[col] = display[col].map(lambda v: f"{v:+.2f}%" if pd.notna(v) else "—")
        _dataframe(display)


def render():
    with st.sidebar:
        st.markdown("---")
        st.markdown("#### Block Filters")
        days = st.slider("Look-back days", min_value=1, max_value=30, value=7)
        limit = st.slider("Rows loaded", min_value=100, max_value=3000, value=1000, step=100)

    try:
        df = _load_data(days=days, limit=limit)
    except Exception as exc:
        st.error(f"Could not load blocked opportunities: {exc}")
        return

    if df.empty:
        st.info("No blocked opportunities recorded yet.")
        return

    tickers = sorted(t for t in df["ticker"].dropna().unique())
    stages = sorted((s for s in df["block_stage"].dropna().unique()), key=_stage_sort_key)
    severities = sorted(s for s in df["runner_severity"].dropna().unique() if s)

    page_header(
        "Blocked Opportunities",
        "The full decision surface behind non-trades: gate stops, replayed runners, near-threshold cases, and repeated ticker vetoes.",
        eyebrow="Decision Observatory",
        pills=[
            status_pill(f"{len(df):,} blocked", "info"),
            status_pill(f"{df['replay_checked_at'].notna().sum():,} replayed", "positive" if df["replay_checked_at"].notna().any() else "neutral"),
            status_pill(f"{(df['runner_severity'] == 'runner').sum():,} runners", "warning"),
        ],
    )

    c_filter1, c_filter2, c_filter3 = st.columns(3)
    with c_filter1:
        selected_tickers = st.multiselect("Ticker", tickers, default=[])
    with c_filter2:
        selected_stages = st.multiselect("Stage", stages, default=[])
    with c_filter3:
        selected_severities = st.multiselect("Runner severity", severities, default=[])

    filtered = _filter_df(df, selected_tickers, selected_stages, selected_severities)
    checked = filtered[filtered["replay_checked_at"].notna()]

    k1, k2, k3, k4, k5 = st.columns(5)
    metric_card(k1, "Blocked", f"{len(filtered):,}", tone="info")
    metric_card(k2, "Replayed", f"{len(checked):,}", tone="positive" if len(checked) else "neutral")
    metric_card(k3, "Missed runners", f"{(filtered['runner_severity'] == 'runner').sum():,}", tone="warning")
    metric_card(k4, "Near threshold", f"{filtered['near_threshold'].sum():,}")
    metric_card(k5, "Tickers", f"{filtered['ticker'].nunique():,}")

    _render_stage_breakdown(filtered)
    st.markdown("---")
    _render_eod_gate_activity()
    st.markdown("---")
    _render_missed_runners(filtered)
    st.markdown("---")
    _render_near_threshold(filtered)
    st.markdown("---")
    _render_direction_errors(filtered)
    st.markdown("---")
    _render_most_vetoed(filtered)
