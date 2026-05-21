from backend.learning import gate_controller


def test_gate_controller_emits_threshold_suggestion(monkeypatch):
    logs = []
    rows = [
        {
            "block_stage": "entry_quality",
            "block_reason": "late_chase",
            "reference_price": 100,
            "close_after_pct": 0.6,
            "max_favorable_pct": 1.2,
            "max_adverse_pct": -0.2,
        }
        for _ in range(8)
    ]

    monkeypatch.setattr(gate_controller, "get_blocked_opportunities", lambda days=7, limit=500: rows)
    monkeypatch.setattr(gate_controller, "log_event", lambda level, event, detail=None: logs.append((event, detail)))

    suggestions = gate_controller.run_gate_controller(days=7, limit=500)

    assert len(suggestions) == 1
    assert suggestions[0]["suggestion"] == "review_threshold_too_tight"
    assert any(event == "gate_adaptation_suggestion" for event, _ in logs)


def test_b_shadow_promotion_is_advisory_only(monkeypatch):
    logs = []
    rows = [
        {"ticker": "AMD", "setup_grade": "B", "net_pnl_pct": 0.01, "created_at": "2026-05-21T14:00:00Z"}
        for _ in range(30)
    ]

    class _Result:
        data = rows

    class _Query:
        def select(self, *args, **kwargs):
            return self
        def eq(self, *args, **kwargs):
            return self
        @property
        def not_(self):
            return self
        def is_(self, *args, **kwargs):
            return self
        def order(self, *args, **kwargs):
            return self
        def limit(self, *args, **kwargs):
            return self
        def execute(self):
            return _Result()

    class _DB:
        def table(self, name):
            assert name == "trades"
            return _Query()

    monkeypatch.setattr(gate_controller, "get_client", lambda: _DB())
    monkeypatch.setattr(gate_controller, "log_event", lambda level, event, detail=None: logs.append((event, detail)))

    result = gate_controller.run_b_shadow_promotion_controller(limit=200)

    assert result["suggestions"][0]["suggested_shadow_size_multiplier"] == 0.20
    assert any(event == "b_shadow_promotion_suggestion" for event, _ in logs)
