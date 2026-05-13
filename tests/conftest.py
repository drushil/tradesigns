"""
tests/conftest.py

Stubs heavy dependencies so backend.agent can be imported in a bare Python
environment (local dev, CI without full pip install).
"""
import os
import pathlib
import sys
import types
from unittest.mock import MagicMock

_REPO_ROOT = pathlib.Path(__file__).parent.parent


def _mod(name: str) -> types.ModuleType:
    """Create a real ModuleType that acts as a stub."""
    m = types.ModuleType(name)
    m.__spec__ = None
    return m


def _magic_mod(name: str) -> MagicMock:
    """MagicMock that passes isinstance(x, types.ModuleType)-style checks."""
    m = MagicMock(spec=types.ModuleType)
    m.__name__ = name
    m.__package__ = name.split(".")[0]
    m.__spec__ = None
    m.__path__ = []
    m.__file__ = f"<stub:{name}>"
    return m


def _ensure(name: str, attrs: dict = None):
    if name not in sys.modules:
        m = _mod(name)
        for k, v in (attrs or {}).items():
            setattr(m, k, v)
        sys.modules[name] = m


# ── dotenv ────────────────────────────────────────────────────────────────────
_ensure("dotenv", {"load_dotenv": lambda *a, **kw: None,
                   "dotenv_values": lambda *a, **kw: {}})

# ── numpy ─────────────────────────────────────────────────────────────────────
try:
    import numpy  # noqa: F401
except ModuleNotFoundError:
    np = _mod("numpy")
    np.nan = float("nan")
    np.inf = float("inf")
    np.array = MagicMock(return_value=[])
    np.mean = MagicMock(return_value=0.0)
    np.std = MagicMock(return_value=1.0)
    np.isnan = MagicMock(return_value=False)
    np.zeros = MagicMock(return_value=[])
    sys.modules["numpy"] = np

# ── pandas ────────────────────────────────────────────────────────────────────
try:
    import pandas  # noqa: F401
except ModuleNotFoundError:
    pd = _mod("pandas")
    pd.DataFrame = MagicMock
    pd.Series = MagicMock
    pd.date_range = MagicMock(return_value=[])
    pd.to_datetime = MagicMock()
    pd.isna = MagicMock(return_value=False)
    pd.Timestamp = MagicMock
    sys.modules["pandas"] = pd
    sys.modules["pandas.core"] = _mod("pandas.core")
    sys.modules["pandas.core.frame"] = _mod("pandas.core.frame")

# ── yfinance ──────────────────────────────────────────────────────────────────
_ensure("yfinance", {"download": MagicMock(return_value=MagicMock(empty=True))})

# ── Alpaca SDK ────────────────────────────────────────────────────────────────
for _mod_name in [
    "alpaca", "alpaca.trading", "alpaca.trading.client",
    "alpaca.trading.requests", "alpaca.trading.enums",
    "alpaca.data", "alpaca.data.historical", "alpaca.data.models",
    "alpaca.data.requests", "alpaca.data.timeframe",
]:
    _ensure(_mod_name)

# ── Groq ──────────────────────────────────────────────────────────────────────
for _mod_name in ["groq", "groq.types", "groq.types.chat"]:
    _ensure(_mod_name)

# ── Anthropic ─────────────────────────────────────────────────────────────────
_ensure("anthropic")

# ── Supabase / postgrest / gotrue ─────────────────────────────────────────────
for _mod_name in ["supabase", "postgrest", "postgrest.exceptions",
                  "supabase.client", "gotrue", "storage3"]:
    _ensure(_mod_name)

# ── requests ──────────────────────────────────────────────────────────────────
try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    req = _mod("requests")
    req.get = MagicMock(return_value=MagicMock(json=lambda: {}, status_code=200))
    req.post = MagicMock(return_value=MagicMock(json=lambda: {}, status_code=200))
    req.exceptions = _mod("requests.exceptions")
    req.exceptions.RequestException = Exception
    sys.modules["requests"] = req
    sys.modules["requests.exceptions"] = req.exceptions

# ── apscheduler ───────────────────────────────────────────────────────────────
for _mod_name in [
    "apscheduler", "apscheduler.schedulers",
    "apscheduler.schedulers.blocking",
    "apscheduler.triggers", "apscheduler.triggers.cron",
]:
    _ensure(_mod_name)

# ── bs4 / BeautifulSoup ───────────────────────────────────────────────────────
try:
    import bs4  # noqa: F401
except ModuleNotFoundError:
    _bs4 = _mod("bs4")
    _bs4.BeautifulSoup = MagicMock
    sys.modules["bs4"] = _bs4

# ── misc optional deps ────────────────────────────────────────────────────────
for _mod_name in ["newsapi", "newsapi.newsapi_client", "praw", "fredapi"]:
    _ensure(_mod_name)

# ── numpy.isscalar — needed by pytest.approx ─────────────────────────────────
if "numpy" in sys.modules and not hasattr(sys.modules["numpy"], "isscalar"):
    sys.modules["numpy"].isscalar = lambda x: isinstance(x, (int, float, complex, bool))

# ── backend package — real __path__ so disk submodules (agent, grading…) import ──
# Must be registered BEFORE backend.signals.engine stub so the stub wins.
if "backend" not in sys.modules:
    _backend_pkg = _mod("backend")
    _backend_pkg.__path__ = [str(_REPO_ROOT / "backend")]
    _backend_pkg.__package__ = "backend"
    sys.modules["backend"] = _backend_pkg
elif not hasattr(sys.modules["backend"], "__path__"):
    sys.modules["backend"].__path__ = [str(_REPO_ROOT / "backend")]

# ── backend.signals.engine — stub entirely (Python 3.8 type-hint incompatibility) ──
if "backend.signals.engine" not in sys.modules:
    _sig = _mod("backend.signals.engine")
    for _fn in [
        "compute_all_signals", "compute_swing_score", "compute_atr",
        "detect_momentum_swing", "detect_regime", "detect_macro_regime",
        "opening_range_breakout_score", "compute_vwap_score", "compute_rsi",
        "is_regular_us_market_hours", "latest_macro_headlines", "scan_for_macro_shock",
    ]:
        setattr(_sig, _fn, MagicMock(return_value={}))
    _sig.is_regular_us_market_hours = MagicMock(return_value=True)
    _sig.latest_macro_headlines = MagicMock(return_value=[])
    _sig.scan_for_macro_shock = MagicMock(return_value=None)
    _sig_pkg = _mod("backend.signals")
    _sig_pkg.__path__ = [str(_REPO_ROOT / "backend" / "signals")]
    sys.modules["backend.signals"] = _sig_pkg
    sys.modules["backend.signals.engine"] = _sig

# ── database.client — stub all functions agent.py imports ─────────────────────
if "database.client" not in sys.modules:
    _db = _mod("database.client")
    _db.get_client = MagicMock(return_value=MagicMock())
    _db.insert_trade = MagicMock(return_value={})
    _db.insert_signal = MagicMock(return_value={})
    _db.insert_blocked_opportunity = MagicMock(return_value={})
    _db.get_unchecked_blocked_opportunities = MagicMock(return_value=[])
    _db.update_blocked_opportunity_replay = MagicMock(return_value={})
    _db.update_signal = MagicMock(return_value={})
    _db.get_signal_percentiles = MagicMock(return_value={})
    _db.upsert_signal_percentiles = MagicMock(return_value={})
    _db.save_open_trade = MagicMock(return_value={})
    _db.get_open_trade_records = MagicMock(return_value=[])
    _db.close_open_trade_record = MagicMock(return_value=None)
    _db.get_recent_trades = MagicMock(return_value=[])
    _db.get_trade_stats = MagicMock(return_value={})
    _db.insert_portfolio_snapshot = MagicMock(return_value={})
    _db.log_event = MagicMock(return_value={})
    _db.get_agent_logs = MagicMock(return_value=[])
    _db.get_unchecked_closed_trades_for_replay = MagicMock(return_value=[])
    _db.update_trade_post_exit_replay = MagicMock(return_value={})
    _db.insert_portfolio_review = MagicMock(return_value={})
    _db.get_portfolio_reviews = MagicMock(return_value=[])
    # Additional functions imported by agent.py
    _db.save_signal_weights = MagicMock(return_value={})
    _db.get_latest_weights = MagicMock(return_value={})
    _db.save_snapshot = MagicMock(return_value={})
    _db.save_learning = MagicMock(return_value={})
    _db.get_logs = MagicMock(return_value=[])
    _db_pkg = _mod("database")
    _db_pkg.__path__ = [str(_REPO_ROOT / "database")]
    _db_pkg.client = _db
    sys.modules["database"] = _db_pkg
    sys.modules["database.client"] = _db
