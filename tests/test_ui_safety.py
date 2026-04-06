from ui.ui_safety import (
    DestructiveActionGuard,
    build_account_close_confirmation,
    build_strategy_close_confirmation,
    build_strategy_stop_confirmation,
    format_runtime_status,
)


class _WidgetStub:
    def __init__(self):
        self.enabled = True

    def setEnabled(self, value: bool):
        self.enabled = bool(value)


class TestDestructiveActionGuard:
    def test_run_disables_and_restores_widgets(self):
        guard = DestructiveActionGuard()
        widget_a = _WidgetStub()
        widget_b = _WidgetStub()
        called = []

        result = guard.run([widget_a, widget_b], lambda: called.append("done"))

        assert result is True
        assert called == ["done"]
        assert widget_a.enabled is True
        assert widget_b.enabled is True
        assert guard.active is False

    def test_second_run_is_blocked_while_active(self):
        guard = DestructiveActionGuard()
        guard._active = True

        result = guard.run([], lambda: None)

        assert result is False

    def test_debounce_blocks_rapid_repeat(self):
        guard = DestructiveActionGuard(debounce_window_sec=10.0)
        called = []

        assert guard.run([], lambda: called.append("first")) is True
        assert guard.run([], lambda: called.append("second")) is False
        assert called == ["first"]


class TestUiSafetyHelpers:
    def test_format_runtime_status_manual_intervention(self):
        text, color = format_runtime_status("active", "manual_intervention_required", "stale")

        assert "manual intervention" in text
        assert color == "#f38ba8"

    def test_strategy_confirmation_mentions_scope(self):
        text = build_strategy_close_confirmation("sid-1", ticker="SBER", quantity=2)

        assert "sid-1" in text
        assert "SBER" in text
        assert "только лоты этой стратегии" in text

    def test_account_confirmation_mentions_account_scope(self):
        text = build_account_close_confirmation()

        assert "ВСЕ открытые позиции счёта" in text
        assert "account-level" in text

    def test_stop_confirmation_warns_about_open_position(self):
        text = build_strategy_stop_confirmation("sid-1")

        assert "sid-1" in text
        assert "не будет закрыта автоматически" in text