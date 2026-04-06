from core import equity_tracker


def test_max_drawdown_is_absolute_and_per_unit_is_secondary(monkeypatch, tmp_path):
    monkeypatch.setattr(equity_tracker, "DATA_DIR", tmp_path)
    equity_tracker._cache.clear()

    equity_tracker.record_equity("agent-1", 1000.0, position_qty=4)
    equity_tracker.record_equity("agent-1", 800.0, position_qty=4)

    state = equity_tracker.get_equity_state("agent-1")

    assert equity_tracker.get_max_drawdown("agent-1") == 200.0
    assert state["max_drawdown"] == 200.0
    assert state["max_drawdown_per_unit"] == 50.0


def test_legacy_per_unit_drawdown_is_migrated_out_of_primary_metric(monkeypatch, tmp_path):
    monkeypatch.setattr(equity_tracker, "DATA_DIR", tmp_path)
    equity_tracker._cache.clear()
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "agent-legacy.json").write_text(
        '{"peak": 1000.0, "max_drawdown": 75.0, "last_equity": 925.0, "samples": 2}',
        encoding="utf-8",
    )

    state = equity_tracker.get_equity_state("agent-legacy")

    assert state["max_drawdown"] == 0.0
    assert state["max_drawdown_per_unit"] == 75.0