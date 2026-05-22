"""
backend/execution/gates.py
Entry precision gates and advisory helpers extracted from agent.py.
These functions perform signal-level, regime-level, and structural
checks before an order is submitted.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone
from typing import Optional

from backend.runtime.env import _env_int, _env_float, _env_bool, _env_value
import backend.runtime.state as state

from backend.execution.common import (_signal_score, _directional_score, _parse_dt,
                                       _trade_pnl_pct, _is_probe_ev_decision,
                                       _strategy_family)
from backend.execution.evidence import enrich_setup_context
from backend.market.sector import (_INDEX_OR_ETF_TICKERS, _INVERSE_ETFS,
                                    _ticker_theme, _is_leveraged_etf,
                                    _sector_default_tickers)
from backend.market.timing import _minutes_since_regular_open
from database.client import (insert_blocked_opportunity, get_logs, log_event)
from backend.grading.engine import (SetupGrade, compute_percentile_thresholds,
                                     grade_setup, compute_sector_confirmation,
                                     get_ticker_percentile_rank,
                                     grade_sort_key)


def _alignment_veto(ticker: str, action: str, signals: dict, profile: dict) -> Optional[dict]:
    """Block obvious signal conflicts before LLM/execution can paper over them."""
    action = str(action or "").upper()
    direction = 1 if action == "BUY" else -1
    ticker = str(ticker or "").upper()
    orb = _signal_score(signals, "orb")
    tape = _signal_score(signals, "tape_aggression")
    put_call = _signal_score(signals, "put_call_ratio")
    rsi = _signal_score(signals, "rsi_divergence")
    macd = _signal_score(signals, "macd_crossover")
    rel = _signal_score(signals, "relative_strength")
    vwap = _signal_score(signals, "vwap_deviation")
    news = _signal_score(signals, "news_sentiment")

    orb_veto = float(profile.get("alignment_orb_veto_threshold", 0.50))
    tape_veto = float(profile.get("alignment_tape_veto_threshold", 0.40))
    options_veto = float(profile.get("alignment_put_call_veto_threshold", 0.50))
    rsi_veto = float(profile.get("alignment_rsi_veto_threshold", 0.50))

    checks = [
        ("orb", orb * direction, orb_veto),
        ("tape_aggression", tape * direction, tape_veto),
    ]
    for name, directional_score, threshold in checks:
        if directional_score < -abs(threshold):
            return {
                "reason": f"signal_alignment_veto_{name}",
                "signal": name,
                "score": round(directional_score, 4),
                "threshold": -abs(threshold),
            }

    if ticker in _INDEX_OR_ETF_TICKERS and put_call * direction < -abs(options_veto):
        return {
            "reason": "signal_alignment_veto_put_call_ratio",
            "signal": "put_call_ratio",
            "score": round(put_call * direction, 4),
            "threshold": -abs(options_veto),
        }

    bullish_confirmers = sum(
        1 for score in (tape, rel, vwap, orb, news)
        if score * direction > 0.15
    )
    if rsi * direction < -abs(rsi_veto) and macd * direction > 0.25 and bullish_confirmers <= 1:
        return {
            "reason": "signal_alignment_veto_rsi_macd_only",
            "signal": "rsi_divergence",
            "score": round(rsi * direction, 4),
            "macd": round(macd * direction, 4),
            "bullish_confirmers": bullish_confirmers,
        }
    return None


def _signal_consensus_block(action: str, signals: dict, regime: str,
                            profile: dict) -> Optional[dict]:
    """Require broad directional agreement instead of one loud signal."""
    direction = 1 if str(action or "").upper() == "BUY" else -1
    min_strength = abs(float(profile.get("signal_consensus_min_strength", 0.15)))
    min_count = int(profile.get("signal_consensus_min_count", 3))
    is_ranging = str(regime or "").lower() == "ranging"
    if is_ranging and bool(profile.get("ranging_core_consensus_enabled", True)):
        core_names = [
            "macd_crossover",
            "relative_strength",
            "tape_aggression",
            "vwap_deviation",
        ]
        aligned_core = []
        opposed_core = []
        observed_core = []
        for name in core_names:
            if name not in (signals or {}):
                continue
            directional_score = _signal_score(signals, name) * direction
            observed_core.append({"signal": name, "score": round(directional_score, 4)})
            if directional_score >= min_strength:
                aligned_core.append(name)
            elif directional_score <= -min_strength:
                opposed_core.append(name)

        core_min_count = int(profile.get("ranging_core_consensus_min_count", 2))
        if core_min_count <= 0 or len(aligned_core) >= core_min_count:
            return None
        return {
            "reason": "ranging_core_consensus_veto",
            "aligned_count": len(aligned_core),
            "min_count": core_min_count,
            "min_strength": min_strength,
            "aligned_signals": aligned_core,
            "opposed_signals": opposed_core,
            "observed_signals": observed_core,
            "core_signals": core_names,
        }

    if is_ranging:
        min_count = int(profile.get("ranging_signal_consensus_min_count", 4))
    if min_count <= 0:
        return None

    signal_names = [
        "rsi_divergence",
        "vwap_deviation",
        "news_sentiment",
        "tape_aggression",
        "order_book_imbalance",
        "orb",
        "macd_crossover",
        "relative_strength",
        "put_call_ratio",
    ]
    aligned = []
    opposed = []
    observed = []
    for name in signal_names:
        if name not in (signals or {}):
            continue
        directional_score = _signal_score(signals, name) * direction
        observed.append({"signal": name, "score": round(directional_score, 4)})
        if directional_score >= min_strength:
            aligned.append(name)
        elif directional_score <= -min_strength:
            opposed.append(name)

    if len(aligned) < min_count:
        return {
            "reason": "signal_consensus_veto",
            "aligned_count": len(aligned),
            "min_count": min_count,
            "min_strength": min_strength,
            "aligned_signals": aligned,
            "opposed_signals": opposed,
            "observed_signals": observed,
        }
    return None


def _reward_risk_block(stop_pct: float, take_profit_pct: float, regime: str,
                       profile: dict) -> Optional[dict]:
    """Block trades whose real order stop/target structure has poor payoff."""
    try:
        stop_pct = abs(float(stop_pct or 0))
        take_profit_pct = abs(float(take_profit_pct or 0))
    except (TypeError, ValueError):
        stop_pct = 0.0
        take_profit_pct = 0.0
    min_rr = float(profile.get("min_reward_risk_ratio", 1.5))
    if str(regime or "").lower() == "ranging":
        min_rr = float(profile.get("ranging_min_reward_risk_ratio", 2.0))
    if min_rr <= 0:
        return None
    rr = take_profit_pct / stop_pct if stop_pct > 0 else 0.0
    if stop_pct <= 0 or take_profit_pct <= 0 or rr < min_rr:
        return {
            "reason": "reward_risk_veto",
            "stop_pct": round(stop_pct, 4),
            "take_profit_pct": round(take_profit_pct, 4),
            "reward_risk": round(rr, 4),
            "min_reward_risk": min_rr,
        }
    return None


def _theme_open_exposure_block(ticker: str, profile: dict) -> Optional[dict]:
    max_open = int(profile.get("max_open_positions_per_theme", 2))
    if max_open <= 0:
        return None
    theme = _ticker_theme(ticker)
    open_same_theme = []
    for open_ticker, trade in (state._open_trades or {}).items():
        if str(trade.get("status") or "open").lower() == "closed":
            continue
        if _ticker_theme(open_ticker) == theme:
            open_same_theme.append(str(open_ticker).upper())
    if len(open_same_theme) >= max_open:
        return {
            "reason": "theme_open_exposure_cap",
            "theme": theme,
            "open_theme_positions": sorted(open_same_theme),
            "max_open_positions_per_theme": max_open,
        }
    return None


def _csv_upper_set(value: str) -> set[str]:
    return {item.strip().upper() for item in str(value or "").split(",") if item.strip()}


def _ranging_probe_decision(ticker: str, setup_context: dict, ev_result: dict,
                            grade: str, profile: dict, block_reason: str,
                            signals_snap: dict = None) -> dict:
    """Decide whether a strict ranging-regime block should become a tiny probe."""
    setup_context = setup_context or {}
    ev_result = ev_result or {}
    signals_snap = signals_snap or {}
    grade = str(grade or "").upper()
    action = str(setup_context.get("action") or "BUY").upper()
    direction = 1 if action == "BUY" else -1
    allowed_grades = _csv_upper_set(profile.get("ranging_probe_allowed_grades", "A+,A"))
    shadow_grades = _csv_upper_set(profile.get("ranging_probe_shadow_grades", "B"))
    shadow_only = grade in shadow_grades and grade not in allowed_grades
    theme = str(setup_context.get("theme") or _ticker_theme(ticker)).lower()
    blocked_themes = {item.lower() for item in _csv_upper_set(profile.get("ranging_probe_blocked_themes", ""))}

    def reject(reason: str, probe_eligible: bool = True, **detail) -> dict:
        payload = {
            "allowed": False,
            "probe_eligible": probe_eligible,
            "reason_not_probed": reason,
            "block_reason": block_reason,
            "grade": grade,
            "theme": theme,
            **detail,
        }
        setup_context["probe_eligible"] = probe_eligible
        setup_context["reason_not_probed"] = reason
        setup_context["ranging_probe_detail"] = payload
        return payload

    if not bool(profile.get("ranging_probe_enabled", False)):
        return reject("ranging_probe_disabled", probe_eligible=False)
    if grade not in allowed_grades and not shadow_only:
        return reject(
            "grade_not_probe_allowed",
            probe_eligible=False,
            allowed_grades=sorted(allowed_grades),
            shadow_grades=sorted(shadow_grades),
        )
    if block_reason not in {
        "ranging_regime_grade_veto",
        "ranging_regime_a_plus_quality_veto",
        "ranging_regime_a_grade_quality_veto",
    }:
        return reject("block_reason_not_probeable")
    if theme in blocked_themes:
        return reject("theme_probe_blocked")

    try:
        net_ev = float(ev_result.get("net_ev_pct"))
    except (TypeError, ValueError):
        net_ev = None
    min_ev = float(profile.get("ranging_probe_min_ev_pct", 0.03))
    if net_ev is None or net_ev < min_ev:
        return reject("ev_below_probe_min", net_ev_pct=net_ev, min_ev_pct=min_ev)

    composite = abs(float(setup_context.get("composite") or 0))
    min_composite = float(profile.get("ranging_probe_min_composite", 0.20))
    if shadow_only and grade == "B":
        min_composite = max(
            min_composite,
            float(profile.get("ranging_probe_grade_b_min_composite", 0.40)),
        )
    if composite < min_composite:
        return reject("composite_below_probe_min", composite=round(composite, 4), min_composite=min_composite)

    breakout_quality = float(setup_context.get("breakout_quality") or 0)
    min_breakout = float(profile.get("ranging_probe_min_breakout_quality", 0.35))
    if shadow_only and grade == "B":
        min_breakout = max(
            min_breakout,
            float(profile.get("ranging_probe_grade_b_min_breakout_quality", 0.60)),
        )
    if breakout_quality < min_breakout:
        return reject(
            "breakout_quality_below_probe_min",
            breakout_quality=round(breakout_quality, 4),
            min_breakout_quality=min_breakout,
        )

    sector_momentum = setup_context.get("sector_momentum") or {}
    relative_pct = sector_momentum.get("relative_pct")
    if relative_pct is not None:
        min_relative = float(profile.get("ranging_probe_min_sector_relative_pct", -0.50))
        if float(relative_pct) < min_relative:
            return reject(
                "sector_relative_strength_too_weak",
                sector_relative_pct=round(float(relative_pct), 4),
                min_sector_relative_pct=min_relative,
            )

    directional = {
        "macd_crossover": _signal_score(signals_snap, "macd_crossover") * direction,
        "tape_aggression": _signal_score(signals_snap, "tape_aggression") * direction,
        "relative_strength": _signal_score(signals_snap, "relative_strength") * direction,
    }
    max_tape_against = abs(float(profile.get("ranging_probe_max_tape_against", 0.05)))
    if directional["tape_aggression"] < -max_tape_against:
        return reject(
            "tape_opposes_probe",
            tape_aggression=round(directional["tape_aggression"], 4),
            max_tape_against=-max_tape_against,
        )

    thresholds = {
        "macd_crossover": float(profile.get("ranging_probe_min_macd", 0.10)),
        "tape_aggression": float(profile.get("ranging_probe_min_tape", 0.10)),
        "relative_strength": float(profile.get("ranging_probe_min_relative_strength", 0.25)),
    }
    aligned = [name for name, score in directional.items() if score >= thresholds[name]]
    min_aligned = int(profile.get("ranging_probe_min_aligned_signals", 2))
    if len(aligned) < min_aligned:
        return reject(
            "too_few_probe_momentum_signals",
            aligned_signals=aligned,
            aligned_count=len(aligned),
            min_aligned_signals=min_aligned,
            directional_scores={k: round(v, 4) for k, v in directional.items()},
        )

    size_multiplier = float(profile.get("ranging_probe_size_multiplier", 0.35))
    if shadow_only:
        shadow_size_multiplier = float(profile.get("ranging_probe_grade_b_shadow_size_multiplier", 0.20))
        setup_context["probe_eligible"] = True
        setup_context["reason_not_probed"] = "b_grade_shadow_only"
        setup_context["ranging_probe_shadow"] = True
        setup_context["ranging_probe_detail"] = {
            "allowed": False,
            "probe_eligible": True,
            "reason_not_probed": "b_grade_shadow_only",
            "grade": grade,
            "block_reason": block_reason,
            "aligned_signals": aligned,
            "directional_scores": {k: round(v, 4) for k, v in directional.items()},
            "net_ev_pct": net_ev,
            "hypothetical_size_multiplier": shadow_size_multiplier,
            "theme": theme,
            "sector_relative_pct": relative_pct,
            "composite": round(composite, 4),
            "breakout_quality": round(breakout_quality, 4),
            "promotion_gate": {
                "min_samples": int(profile.get("ranging_probe_grade_b_promote_min_samples", 8)),
                "min_win_rate": float(profile.get("ranging_probe_grade_b_promote_min_win_rate", 0.55)),
                "requires_avg_mfe_gt_abs_avg_mae": bool(
                    profile.get("ranging_probe_grade_b_promote_requires_mfe_gt_mae", True)
                ),
                "promote_size_multiplier": float(
                    profile.get("ranging_probe_grade_b_promote_size_multiplier", 0.20)
                ),
            },
        }
        return setup_context["ranging_probe_detail"]

    current_multiplier = float(ev_result.get("size_multiplier") or 1.0)
    ev_result["size_multiplier"] = min(current_multiplier, size_multiplier)
    ev_result["ev_decision"] = "ranging_regime_probe"
    ev_result["decision"] = "proceed"
    ev_result["ranging_probe"] = True
    setup_context["ranging_probe"] = True
    setup_context["probe_eligible"] = True
    setup_context["reason_not_probed"] = None
    setup_context["ranging_probe_detail"] = {
        "grade": grade,
        "block_reason_overridden": block_reason,
        "aligned_signals": aligned,
        "directional_scores": {k: round(v, 4) for k, v in directional.items()},
        "net_ev_pct": net_ev,
        "size_multiplier": ev_result["size_multiplier"],
        "theme": theme,
        "sector_relative_pct": relative_pct,
    }
    return {"allowed": True, **setup_context["ranging_probe_detail"]}


def _ranging_regime_block(ticker: str, setup_context: dict, ev_result: dict,
                          setup_grade: Optional[SetupGrade], profile: dict,
                          signals_snap: dict = None) -> Optional[dict]:
    if str((setup_context or {}).get("intraday_regime", "")).lower() != "ranging":
        return None
    grade = setup_grade.grade if setup_grade else (setup_context or {}).get("setup_grade")
    breakout_quality = float((setup_context or {}).get("breakout_quality") or 0)
    net_ev = (ev_result or {}).get("net_ev_pct")
    net_ev = float(net_ev) if net_ev is not None else None
    min_grade = str(profile.get("ranging_min_grade_required", "A+")).upper()
    if grade_sort_key(grade or "C") < grade_sort_key(min_grade):
        probe = _ranging_probe_decision(
            ticker, setup_context, ev_result, grade, profile,
            "ranging_regime_grade_veto", signals_snap,
        )
        if probe.get("allowed"):
            return None
        block = {
            "reason": "ranging_regime_grade_veto",
            "grade": grade,
            "min_grade": min_grade,
            "breakout_quality": round(breakout_quality, 4),
        }
        block["probe"] = probe
        return block

    if _is_leveraged_etf(ticker, profile):
        min_lev_ev = float(profile.get("ranging_leveraged_min_ev_pct", 0.25))
        if net_ev is None or net_ev < min_lev_ev:
            return {
                "reason": "ranging_regime_leveraged_ev_veto",
                "net_ev_pct": net_ev,
                "min_ev_pct": min_lev_ev,
            }

    if grade == "A+":
        composite = abs(float((setup_context or {}).get("composite") or 0))
        min_composite = float(profile.get("ranging_a_plus_min_composite", 0.25))
        min_breakout = float(profile.get("ranging_a_plus_min_breakout_quality", 0.70))
        min_ev = float(profile.get("ranging_a_plus_min_ev_pct", 0.20))
        if composite < min_composite or breakout_quality < min_breakout or net_ev is None or net_ev < min_ev:
            probe = _ranging_probe_decision(
                ticker, setup_context, ev_result, grade, profile,
                "ranging_regime_a_plus_quality_veto", signals_snap,
            )
            if probe.get("allowed"):
                return None
            block = {
                "reason": "ranging_regime_a_plus_quality_veto",
                "grade": grade,
                "composite": round(composite, 4),
                "min_composite": min_composite,
                "breakout_quality": round(breakout_quality, 4),
                "min_breakout_quality": min_breakout,
                "net_ev_pct": net_ev,
                "min_ev_pct": min_ev,
            }
            block["probe"] = probe
            return block

    if grade == "A":
        min_breakout = float(profile.get("ranging_a_grade_min_breakout_quality", 0.80))
        min_ev = float(profile.get("ranging_a_grade_min_ev_pct", 0.25))
        if breakout_quality < min_breakout or net_ev is None or net_ev < min_ev:
            probe = _ranging_probe_decision(
                ticker, setup_context, ev_result, grade, profile,
                "ranging_regime_a_grade_quality_veto", signals_snap,
            )
            if probe.get("allowed"):
                return None
            block = {
                "reason": "ranging_regime_a_grade_quality_veto",
                "grade": grade,
                "breakout_quality": round(breakout_quality, 4),
                "min_breakout_quality": min_breakout,
                "net_ev_pct": net_ev,
                "min_ev_pct": min_ev,
            }
            block["probe"] = probe
            return block

    return None


def _llm_rationale_mentions_conflict(llm_result: dict) -> bool:
    rationale = str((llm_result or {}).get("rationale") or "").lower()
    conflict_terms = ("conflict", "mixed", "disagree", "diverg", "near-zero", "near zero")
    return any(term in rationale for term in conflict_terms)


def _known_negative_grade_override_block(ev_result: dict, profile: dict) -> Optional[dict]:
    ev_result = ev_result or {}
    ev_net_pct = ev_result.get("net_ev_pct")
    if ev_net_pct is None:
        return None
    ev_sample_size = int(ev_result.get("sample_size") or 0)
    min_known_samples = int(profile.get("grade_ev_override_negative_min_samples", 10))
    ev_net_pct = float(ev_net_pct)
    if ev_net_pct < 0 and ev_sample_size >= min_known_samples:
        return {
            "net_ev_pct": ev_net_pct,
            "sample_size": ev_sample_size,
            "min_samples": min_known_samples,
        }
    return None


def _probe_floor_inflation_block(ev_decision: str, grade_min_notional_applied: bool,
                                 intended_size_eur: float, final_size_eur: float,
                                 profile: dict) -> Optional[dict]:
    if not (_is_probe_ev_decision(ev_decision) and grade_min_notional_applied and intended_size_eur > 0):
        return None
    inflation_multiple = float(final_size_eur or 0) / float(intended_size_eur)
    max_inflation = float(profile.get("probe_floor_inflation_max_multiple", 1.25))
    if inflation_multiple > max_inflation:
        return {
            "inflation_multiple": round(inflation_multiple, 3),
            "max_inflation": max_inflation,
        }
    return None


def _late_chase_block(action: str, signals_snap: dict, atr_data: dict, profile: dict) -> Optional[dict]:
    if not bool(profile.get("late_chase_block_enabled", True)):
        return None
    try:
        pct_dev = float(
            ((signals_snap or {}).get("vwap_deviation") or {})
            .get("meta", {})
            .get("pct_deviation")
        )
        atr_pct = float((atr_data or {}).get("atr_pct") or 0)
    except (TypeError, ValueError):
        return None
    if atr_pct <= 0:
        return None

    threshold = atr_pct * float(profile.get("late_chase_atr_mult", 1.5))
    side = str(action or "").upper()
    directionally_extended = (side == "BUY" and pct_dev > threshold) or (side == "SELL" and pct_dev < -threshold)
    if not directionally_extended:
        return None
    return {
        "reason": "late_chase",
        "pct_deviation": round(pct_dev, 4),
        "atr_pct": round(atr_pct, 4),
        "threshold_pct": round(threshold, 4),
        "late_chase_atr_mult": float(profile.get("late_chase_atr_mult", 1.5)),
    }


def _rvol_block(signal_result: dict, profile: dict) -> Optional[dict]:
    if not bool(profile.get("rvol_gate_enabled", True)):
        return None
    rvol = (signal_result or {}).get("rvol_data") or {}
    if not rvol.get("rvol_available"):
        return None
    try:
        ratio = float(rvol.get("rvol_ratio") or 0)
    except (TypeError, ValueError):
        return None
    min_ratio = float(profile.get("rvol_min_multiplier", 1.3))
    if ratio >= min_ratio:
        return None
    return {
        "reason": "low_rvol",
        "rvol_ratio": round(ratio, 4),
        "min_rvol": min_ratio,
        "avg_vol": rvol.get("avg_vol"),
        "current_vol": rvol.get("current_vol"),
        "slot": rvol.get("slot"),
    }


def _vwap_1m_confirmation_downgrade(candidate: dict, setup_grade: SetupGrade,
                                    profile: dict) -> SetupGrade:
    if not bool(profile.get("vwap_1m_confirm_enabled", True)):
        return setup_grade
    if setup_grade is None or setup_grade.grade != "A+":
        return setup_grade

    signals = candidate.get("signals_snap") or {}
    vwap_signal = signals.get("vwap_deviation") or {}
    vwap_meta = vwap_signal.get("meta") or {}
    try:
        price = float(vwap_meta.get("price"))
        vwap = float(vwap_meta.get("vwap"))
        vwap_score = float(vwap_signal.get("score") or 0)
    except (TypeError, ValueError):
        return setup_grade
    if price <= 0 or vwap <= 0:
        return setup_grade

    action = str(candidate.get("action_hint") or "").upper()
    wrong_side = (action == "BUY" and price < vwap) or (action == "SELL" and price > vwap)
    weakly_against = (action == "BUY" and vwap_score > 0.05) or (action == "SELL" and vwap_score < -0.05)
    if not (wrong_side and weakly_against):
        return setup_grade

    downgraded = SetupGrade(
        grade="A",
        size_multiplier=min(float(setup_grade.size_multiplier or 1.0), 1.0),
        partial_exit_pct=max(float(setup_grade.partial_exit_pct or 0.25), 0.40),
        runner_atr_multiplier=min(float(setup_grade.runner_atr_multiplier or 1.0), 1.0),
        allow_leverage=False,
        reasons=list(setup_grade.reasons or []) + ["1m_vwap_not_confirmed"],
        confirmations=setup_grade.confirmations,
        sector_confirmation=setup_grade.sector_confirmation,
        percentile_rank=setup_grade.percentile_rank,
        orb_active=setup_grade.orb_active,
    )
    log_event("INFO", "a_plus_downgraded_1m_confirmation", {
        "ticker": candidate.get("ticker"),
        "action": action,
        "price": round(price, 4),
        "vwap": round(vwap, 4),
        "vwap_score": round(vwap_score, 4),
        "reason": "wrong_side_of_vwap",
    })
    candidate.setdefault("setup_context", {})["a_plus_downgraded_1m_confirmation"] = True
    return downgraded


def _event_risk_active(ticker: str) -> dict:
    try:
        from backend.earnings.scanner import get_cached_earnings_guard
        info = (get_cached_earnings_guard() or {}).get(str(ticker or "").upper(), {}) or {}
        return info if info.get("blocked") else {}
    except Exception:
        return {}


def _overnight_event_risk_active(ticker: str) -> dict:
    """Return cached overnight event/filing risk if the guard has data."""
    info = _event_risk_active(ticker)
    if not info:
        return {}
    days = info.get("days_to_filing")
    try:
        if days is not None and int(days) <= 1:
            return info
    except (TypeError, ValueError):
        pass
    return info if info.get("blocked") else {}


def _breakout_quality(side: str, composite: float, signals: dict, market_regime: str = None) -> float:
    side = str(side or "").upper()
    direction = 1 if side == "BUY" else -1
    macd = _signal_score(signals, "macd_crossover") * direction
    tape = _signal_score(signals, "tape_aggression") * direction
    rel_strength = _signal_score(signals, "relative_strength") * direction
    news = _signal_score(signals, "news_sentiment") * direction
    vwap = _signal_score(signals, "vwap_deviation") * direction
    composite_aligned = float(composite or 0) * direction
    market = str(market_regime or "").lower()
    market_bonus = 1.0 if side == "BUY" and market in {"bull", "transitioning", ""} else 0.5
    if side == "SELL" and market == "bear":
        market_bonus = 1.0

    components = [
        max(0.0, min(macd, 1.0)),
        max(0.0, min(tape, 1.0)),
        max(0.0, min(rel_strength, 1.0)),
        max(0.0, min(news, 1.0)),
        max(0.0, min(abs(vwap) / 0.8, 1.0)) if vwap < 0 else 0.0,
        max(0.0, min(composite_aligned / 0.5, 1.0)),
        market_bonus,
    ]
    return round(sum(components) / len(components), 4)


def _time_of_day_rank_bonus(minutes_since_open: Optional[int]) -> float:
    try:
        minutes = int(minutes_since_open)
    except (TypeError, ValueError):
        return 0.0
    if 15 <= minutes <= 45:
        return 0.10
    if 60 <= minutes <= 120:
        return -0.08
    if 300 <= minutes <= 345:
        return 0.07
    if 360 <= minutes <= 385:
        return 0.05
    return 0.0


def _candidate_rank_score(composite: float, breakout_quality: float, strategy_family: str,
                          event_risk_active: bool = False,
                          minutes_since_open: Optional[int] = None) -> float:
    strategy_bonus = {
        "trend_following": 0.12,
        "signal_composite": 0.04,
        "mean_reversion": -0.08,
        "direct_short": -0.03,
    }.get(str(strategy_family or ""), 0.0)
    event_penalty = 0.08 if event_risk_active else 0.0
    time_bonus = _time_of_day_rank_bonus(minutes_since_open)
    score = (abs(float(composite or 0)) * 0.45) + (float(breakout_quality or 0) * 0.55)
    return round(max(0.0, score + strategy_bonus + time_bonus - event_penalty), 4)


def _trade_setup_context(ticker: str, action: str, composite: float,
                         signals: dict, signal_result: dict,
                         regime_state, gate_reason: str = None) -> dict:
    strategy_family = _strategy_family(
        ticker, action, getattr(regime_state, "intraday_regime", None), signal_result,
        mean_reversion_trade=bool(signal_result.get("mean_reversion_signal")),
    )
    event_info = _event_risk_active(ticker)
    event_probe = bool(
        event_info
        and gate_reason
        and str(gate_reason).startswith("event_risk_intraday_probe")
    )
    breakout_quality = _breakout_quality(
        action, composite, signals, getattr(regime_state, "market_regime", None)
    )
    atr_data = signal_result.get("atr_data") or {}
    minutes_since_open = _minutes_since_regular_open()
    is_leveraged = _is_leveraged_etf(str(ticker or "").upper(), state.PROFILE)
    time_of_day_bonus = _time_of_day_rank_bonus(minutes_since_open)
    context = {
        "ticker": ticker,
        "action": action,
        "composite": float(composite or 0),
        "strategy_family": strategy_family,
        "intraday_regime": getattr(regime_state, "intraday_regime", None),
        "market_regime": getattr(regime_state, "market_regime", None),
        "breakout_quality": breakout_quality,
        "candidate_rank_score": _candidate_rank_score(
            composite, breakout_quality, strategy_family, bool(event_info),
            minutes_since_open=minutes_since_open,
        ),
        "time_of_day_bonus": time_of_day_bonus,
        "event_risk_active": bool(event_info),
        "event_risk_intraday_probe": event_probe,
        "event_risk_info": event_info,
        "minutes_since_open": minutes_since_open,
        "atr_pct": atr_data.get("atr_pct"),
        "volatility_regime": atr_data.get("volatility_regime"),
        "is_leveraged_etf": is_leveraged,
    }
    return enrich_setup_context(ticker, action, signals, signal_result, context)


def _record_blocked_opportunity(ticker: str, action: str, composite: float,
                                signals: dict, setup_context: dict,
                                regime: str, block_stage: str, block_reason: str,
                                ev_result: dict = None, reference_price: float = None,
                                block_detail: dict = None):
    try:
        payload = {
            "ticker": str(ticker or "").upper(),
            "action_hint": action,
            "composite_score": round(float(composite or 0), 4),
            "block_stage": block_stage,
            "block_reason": block_reason,
            "block_detail": block_detail or {},
            "candidate_rank_score": (setup_context or {}).get("candidate_rank_score"),
            "breakout_quality": (setup_context or {}).get("breakout_quality"),
            "ev_decision": (ev_result or {}).get("ev_decision"),
            "ev_net_pct": (ev_result or {}).get("net_ev_pct"),
            "ev_result_json": ev_result or {},
            "signals_json": {k: {"score": v.get("score", 0)} for k, v in (signals or {}).items() if isinstance(v, dict)},
            "setup_context_json": setup_context or {},
            "regime": regime,
            "market_regime": (setup_context or {}).get("market_regime"),
            "strategy_family": (setup_context or {}).get("strategy_family"),
            "playbook": (setup_context or {}).get("playbook"),
            "playbook_lifecycle": (setup_context or {}).get("playbook_lifecycle"),
            "session_window": (setup_context or {}).get("session_window"),
            "primary_factor": (setup_context or {}).get("primary_factor"),
            "factor_bucket": (setup_context or {}).get("factor_bucket"),
            "regime_key": (setup_context or {}).get("regime_key"),
            "data_quality_state": (setup_context or {}).get("data_quality_state"),
            "data_quality_json": (setup_context or {}).get("data_quality") or {},
            "cost_estimate_json": (setup_context or {}).get("cost_estimate") or {},
            "estimated_spread_pct": (setup_context or {}).get("estimated_spread_pct"),
            "estimated_total_cost_pct": (setup_context or {}).get("estimated_total_cost_pct"),
            "event_risk_active": bool((setup_context or {}).get("event_risk_active")),
            "reference_price": reference_price,
            "setup_grade": (setup_context or {}).get("setup_grade"),
            "a_plus_blocked": (setup_context or {}).get("setup_grade") == "A+",
            "minutes_since_open": (setup_context or {}).get("minutes_since_open"),
            "atr_pct": (setup_context or {}).get("atr_pct"),
            "volatility_bucket": (setup_context or {}).get("volatility_regime"),
            "is_leveraged_etf": (setup_context or {}).get("is_leveraged_etf"),
            "probe_eligible": bool((setup_context or {}).get("probe_eligible", False)),
            "reason_not_probed": (setup_context or {}).get("reason_not_probed") or "not_probe_eligible",
        }
        result = insert_blocked_opportunity(payload)
        if result.get("error"):
            return
    except Exception:
        return


def _threshold_block_detail(action: str, composite: float, profile: dict,
                            market_regime: str = None) -> dict:
    """Structured analytics for threshold misses; does not affect gate behavior."""
    action = str(action or "").upper()
    try:
        score = abs(float(composite or 0.0))
    except (TypeError, ValueError):
        score = 0.0
    threshold = float(profile.get("min_signal_score", 0.0) or 0.0)
    if action == "SELL":
        threshold = float(profile.get("min_short_signal_score", threshold) or threshold)
        if str(market_regime or "").lower() == "bull":
            threshold = float(profile.get("bull_short_signal_score", threshold) or threshold)
    gap = threshold - score
    if threshold <= 0 or gap < 0:
        return {}
    margin = _env_float("NEAR_THRESHOLD_MARGIN", 0.01)
    return {
        "kind": "signal_threshold",
        "score": round(score, 4),
        "threshold": round(threshold, 4),
        "threshold_gap": round(gap, 4),
        "near_threshold": gap <= margin,
        "near_threshold_margin": margin,
    }
