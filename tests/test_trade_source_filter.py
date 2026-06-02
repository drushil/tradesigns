import importlib.util
import pathlib
import sys
import types


def _load_real_client():
    if "supabase" not in sys.modules:
        supabase = types.ModuleType("supabase")
        sys.modules["supabase"] = supabase
    sys.modules["supabase"].create_client = lambda *args, **kwargs: None
    path = pathlib.Path(__file__).resolve().parents[1] / "database" / "client.py"
    spec = importlib.util.spec_from_file_location("real_database_client_for_trade_source_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_client = _load_real_client()
_trade_matches_source = _client._trade_matches_source


def test_agent_trade_source_excludes_legacy_manual_order_ids():
    manual = {"ticker": "NVDA", "trade_source": "agent", "order_id": "MANUAL-NVDA-20260601"}
    automated = {"ticker": "META", "trade_source": "agent", "order_id": "alpaca-123"}

    assert _trade_matches_source(manual, "agent") is False
    assert _trade_matches_source(automated, "agent") is True


def test_advisory_manual_source_includes_linked_or_strategy_rows():
    linked = {"ticker": "PLTR", "trade_source": "agent", "advisory_signal_id": 42}
    strategy = {"ticker": "MU", "trade_source": "agent", "strategy_family": "advisory_manual"}

    assert _trade_matches_source(linked, "advisory_manual") is True
    assert _trade_matches_source(strategy, "advisory_manual") is True
    assert _trade_matches_source(linked, "agent") is False
    assert _trade_matches_source(strategy, "agent") is False


def test_all_trade_source_keeps_both_agent_and_manual_rows():
    rows = [
        {"ticker": "NVDA", "order_id": "MANUAL-NVDA-20260601"},
        {"ticker": "META", "trade_source": "agent"},
    ]

    assert [_trade_matches_source(row, "all") for row in rows] == [True, True]
