"""
backend/runtime/state.py
Mutable agent runtime state shared across all sub-modules.

IMPORTANT — import as a module, never destructure with "from":
    import backend.runtime.state as state
    ...
    state._open_trades[ticker] = {...}   # mutation propagates to all importers
    state.PROFILE = new_profile          # rebinding propagates correctly

"from backend.runtime.state import _open_trades" is WRONG for mutable containers
because re-binding (state.PROFILE = x) in one module won't update a local reference
that another module captured at import time.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Agent configuration (set once at startup from env/profile)
# ---------------------------------------------------------------------------

# List of tickers in the active universe — populated by agent.py at startup
# after sector config is resolved.
TICKERS: list[str] = []
SWING_TICKERS: list[str] = []

# Active risk profile dict
PROFILE: dict = {}

# Investment horizon — "short" | "mid" | "both"
HORIZON: str = "short"

# LLM rate-limit cap per hour
LLM_HOUR_LIMIT: int = 20

# True when running against Alpaca paper account
IS_PAPER_TRADING: bool = True

# ---------------------------------------------------------------------------
# Learning engine (singleton, persists across cycles)
# ---------------------------------------------------------------------------
_learning_engine: Optional[Any] = None   # RegimeAwareWeightEngine | None

# ---------------------------------------------------------------------------
# LLM rate-limit counters (reset hourly)
# ---------------------------------------------------------------------------
_llm_calls_this_hour: int = 0
_llm_hour_reset: datetime = datetime.utcnow()

# ---------------------------------------------------------------------------
# Open trade registries
# ---------------------------------------------------------------------------
# {ticker: {entry_price, entry_time, stop_price, hold_minutes, ...}}
_open_trades: dict[str, dict] = {}

# {ticker: {entry_price, entry_time, hold_days, ...}}
_swing_trades: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Macro-shock cache
# ---------------------------------------------------------------------------
_last_shock_refresh: Optional[datetime] = None
_last_shock_result: dict = {
    "shock_detected": False,
    "classification": "NORMAL",
    "affected_sectors": [],
    "direction": "mixed",
    "reason": "not_scanned",
}

# ---------------------------------------------------------------------------
# Day-trade telemetry (PDT rule retired June 2026 — informational only)
# [(date, ticker)] — same-day round trips
# ---------------------------------------------------------------------------
_day_trade_log: list = []

# ---------------------------------------------------------------------------
# Per-cycle signal cache  {ticker: (timestamp, signal_dict)}
# Expires after 8 min (one cycle apart at 10-min cadence)
# ---------------------------------------------------------------------------
_signal_cache: dict[str, tuple[datetime, dict]] = {}
_SIGNAL_CACHE_TTL_SECONDS: int = 480

# ---------------------------------------------------------------------------
# Cross-ticker composites collected during a cycle (sector confirmation)
# ---------------------------------------------------------------------------
_cycle_composites: dict[str, float] = {}

# Percentile baseline from DB — loaded once per cycle
_cycle_db_percentiles: dict[str, dict] = {}
