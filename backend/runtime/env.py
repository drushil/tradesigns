"""
backend/runtime/env.py
Pure environment-variable helpers and FX constants.

No imports from the rest of the agent codebase — safe to import anywhere
without risk of circular imports.

Usage in other modules:
    from backend.runtime.env import _env_bool, _env_float, _eur_to_usd
"""
from __future__ import annotations
import os


def _env_value(key: str, default: str) -> str:
    value = os.getenv(key)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env_value(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env_value(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool = True) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() not in {"false", "0", "no"}


def _eurusd_rate() -> float:
    return _env_float("EURUSD_RATE", 1.08)


def _eur_to_usd(amount_eur: float) -> float:
    return float(amount_eur or 0) * _eurusd_rate()
