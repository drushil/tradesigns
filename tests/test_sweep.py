import backend.sweep.agent as sweep


def test_sweep_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SWEEP_ENABLED", raising=False)

    plan = sweep.compute_sweep_plan({
        "equity_eur": 3000,
        "cash_eur": 3000,
        "open_positions": 0,
        "pending_signals": 0,
    })

    assert plan["enabled"] is False
    assert plan["should_sweep"] is False
    assert plan["reason"] == "disabled"


def test_sweep_enabled_allows_eligible_cash(monkeypatch):
    monkeypatch.setenv("SWEEP_ENABLED", "true")

    plan = sweep.compute_sweep_plan({
        "equity_eur": 3000,
        "cash_eur": 3000,
        "open_positions": 0,
        "pending_signals": 0,
    })

    assert plan["enabled"] is True
    assert plan["should_sweep"] is True
    assert plan["reason"] == "eligible"
