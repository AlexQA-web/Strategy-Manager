# tests/test_order_lifecycle.py

"""
Тесты для OrderLifecycle state machine и PendingOrderRegistry.
"""

from unittest.mock import MagicMock

import pytest

from core.order_lifecycle import (
    OrderLifecycle,
    OrderState,
    PendingOrderRegistry,
)


class TestOrderLifecycle:
    """Тесты order lifecycle state machine."""

    def _make_lifecycle(self, **kwargs):
        defaults = {
            "tid": "12345",
            "strategy_id": "test_agent",
            "ticker": "SiM5",
            "side": "buy",
            "requested_qty": 10,
            "order_type": "market",
        }
        defaults.update(kwargs)
        return OrderLifecycle(**defaults)

    def test_initial_state_is_working(self):
        lc = self._make_lifecycle()
        assert lc.state == OrderState.WORKING
        assert lc.filled_qty == 0
        assert lc.is_terminal is False

    def test_matched_transitions_to_terminal(self):
        lc = self._make_lifecycle()
        lc.update_from_connector("matched", filled=10, avg_price=85000.0)
        assert lc.state == OrderState.MATCHED
        assert lc.filled_qty == 10
        assert lc.avg_price == 85000.0
        assert lc.is_terminal is True

    def test_partial_fill_detected(self):
        lc = self._make_lifecycle()
        lc.update_from_connector("working", filled=5, avg_price=85000.0)
        assert lc.state == OrderState.PARTIAL_FILL
        assert lc.filled_qty == 5

    def test_partial_fill_then_matched(self):
        lc = self._make_lifecycle()
        lc.update_from_connector("working", filled=3)
        assert lc.state == OrderState.PARTIAL_FILL

        lc.update_from_connector("matched", filled=10, avg_price=85100.0)
        assert lc.state == OrderState.MATCHED
        assert lc.filled_qty == 10
        assert lc.terminal_filled == 10

    def test_canceled_is_terminal(self):
        lc = self._make_lifecycle()
        lc.update_from_connector("canceled", filled=0)
        assert lc.state == OrderState.CANCELED
        assert lc.is_terminal is True

    def test_denied_is_terminal(self):
        lc = self._make_lifecycle()
        lc.update_from_connector("denied", filled=0)
        assert lc.state == OrderState.DENIED
        assert lc.is_terminal is True

    def test_canceled_with_partial_fill(self):
        lc = self._make_lifecycle()
        lc.update_from_connector("working", filled=3)
        lc.update_from_connector("canceled", filled=3)
        assert lc.state == OrderState.CANCELED
        assert lc.filled_qty == 3
        assert lc.terminal_filled == 3

    # --- Monotonicity ---

    def test_filled_cannot_decrease(self):
        lc = self._make_lifecycle()
        lc.update_from_connector("working", filled=5)
        result = lc.update_from_connector("working", filled=3)
        assert result == "out_of_order"
        assert lc.filled_qty == 5  # unchanged

    def test_filled_stays_same_is_ok(self):
        lc = self._make_lifecycle()
        lc.update_from_connector("working", filled=5)
        result = lc.update_from_connector("working", filled=5)
        assert result is None
        assert lc.filled_qty == 5

    # --- Late fill detection ---

    def test_late_fill_after_matched(self):
        lc = self._make_lifecycle()
        lc.update_from_connector("matched", filled=7, avg_price=85000.0)
        assert lc.state == OrderState.MATCHED
        assert lc.terminal_filled == 7

        result = lc.update_from_connector("matched", filled=10, avg_price=85050.0)
        assert result == "late_fill"
        assert lc.state == OrderState.LATE_FILL_REPAIR
        assert lc.filled_qty == 10
        assert lc.get_late_fill_delta() == 3

    def test_late_fill_after_canceled(self):
        lc = self._make_lifecycle()
        lc.update_from_connector("canceled", filled=5)
        assert lc.terminal_filled == 5

        result = lc.update_from_connector("matched", filled=8, avg_price=85000.0)
        assert result == "late_fill"
        assert lc.state == OrderState.LATE_FILL_REPAIR
        assert lc.get_late_fill_delta() == 3

    def test_late_fill_after_timeout(self):
        lc = self._make_lifecycle()
        lc.update_from_connector("working", filled=2)
        lc.mark_timeout()
        assert lc.state == OrderState.TIMEOUT
        assert lc.terminal_filled == 2

        result = lc.update_from_connector("matched", filled=10, avg_price=85100.0)
        assert result == "late_fill"
        assert lc.state == OrderState.LATE_FILL_REPAIR
        assert lc.get_late_fill_delta() == 8

    def test_no_late_fill_delta_when_not_repair(self):
        lc = self._make_lifecycle()
        lc.update_from_connector("matched", filled=10)
        assert lc.get_late_fill_delta() == 0

    # --- Timeout ---

    def test_mark_timeout(self):
        lc = self._make_lifecycle()
        lc.update_from_connector("working", filled=3)
        lc.mark_timeout()
        assert lc.state == OrderState.TIMEOUT
        assert lc.is_terminal is True
        assert lc.terminal_filled == 3

    def test_mark_timeout_ignored_if_already_terminal(self):
        lc = self._make_lifecycle()
        lc.update_from_connector("matched", filled=10)
        lc.mark_timeout()
        assert lc.state == OrderState.MATCHED  # unchanged

    # --- Cancel pending ---

    def test_mark_cancel_pending(self):
        lc = self._make_lifecycle()
        lc.mark_cancel_pending()
        assert lc.state == OrderState.CANCEL_PENDING

    def test_cancel_pending_then_canceled(self):
        lc = self._make_lifecycle()
        lc.mark_cancel_pending()
        lc.update_from_connector("canceled", filled=0)
        assert lc.state == OrderState.CANCELED

    def test_cancel_pending_ignored_if_terminal(self):
        lc = self._make_lifecycle()
        lc.update_from_connector("matched", filled=10)
        lc.mark_cancel_pending()
        assert lc.state == OrderState.MATCHED

    # --- Snapshot ---

    def test_snapshot_contains_all_fields(self):
        lc = self._make_lifecycle()
        lc.update_from_connector("working", filled=5, avg_price=85000.0)
        snap = lc.snapshot()

        assert snap["tid"] == "12345"
        assert snap["strategy_id"] == "test_agent"
        assert snap["ticker"] == "SiM5"
        assert snap["side"] == "buy"
        assert snap["requested_qty"] == 10
        assert snap["state"] == "partial_fill"
        assert snap["filled_qty"] == 5
        assert snap["avg_price"] == 85000.0
        assert "age_sec" in snap

    # --- Connector status mapping ---

    def test_removed_maps_to_canceled(self):
        lc = self._make_lifecycle()
        lc.update_from_connector("removed", filled=0)
        assert lc.state == OrderState.CANCELED

    def test_expired_maps_to_canceled(self):
        lc = self._make_lifecycle()
        lc.update_from_connector("expired", filled=0)
        assert lc.state == OrderState.CANCELED

    def test_killed_maps_to_denied(self):
        lc = self._make_lifecycle()
        lc.update_from_connector("killed", filled=0)
        assert lc.state == OrderState.DENIED

    def test_unknown_status_stays_working(self):
        lc = self._make_lifecycle()
        lc.update_from_connector("something_new", filled=0)
        assert lc.state == OrderState.WORKING


class TestPendingOrderRegistry:
    """Тесты PendingOrderRegistry."""

    def test_register_and_get_pending(self):
        reg = PendingOrderRegistry(load_from_storage=False)
        lc = OrderLifecycle("t1", "agent1", "SiM5", "buy", 10)
        reg.register(lc)
        assert len(reg.get_pending()) == 1

    def test_unregister(self):
        reg = PendingOrderRegistry(load_from_storage=False)
        lc = OrderLifecycle("t1", "agent1", "SiM5", "buy", 10)
        reg.register(lc)
        reg.unregister("t1")
        assert len(reg.get_pending()) == 0

    def test_check_late_fills_detects_increase(self):
        reg = PendingOrderRegistry(load_from_storage=False)
        lc = OrderLifecycle("t1", "agent1", "SiM5", "buy", 10)
        lc.update_from_connector("canceled", filled=5)

        reg.register(lc)

        mock_connector = MagicMock()
        mock_connector.get_order_status.return_value = {
            "status": "matched",
            "quantity": 10,
            "balance": 2,
            "avg_price": 85000.0,
        }

        results = reg.check_late_fills(mock_connector)
        assert len(results) == 1
        assert results[0]["delta"] == 3  # was 5, now 8
        assert results[0]["tid"] == "t1"
        assert results[0]["strategy_id"] == "agent1"

    def test_check_late_fills_no_change(self):
        reg = PendingOrderRegistry(load_from_storage=False)
        lc = OrderLifecycle("t1", "agent1", "SiM5", "buy", 10)
        lc.update_from_connector("canceled", filled=5)

        reg.register(lc)

        mock_connector = MagicMock()
        mock_connector.get_order_status.return_value = {
            "status": "canceled",
            "quantity": 10,
            "balance": 5,
            "avg_price": 85000.0,
        }

        results = reg.check_late_fills(mock_connector)
        assert len(results) == 0

    def test_check_late_fills_connector_error_ignored(self):
        reg = PendingOrderRegistry(load_from_storage=False)
        lc = OrderLifecycle("t1", "agent1", "SiM5", "buy", 10)
        lc.update_from_connector("timeout", filled=3)
        lc.mark_timeout()
        reg.register(lc)

        mock_connector = MagicMock()
        mock_connector.get_order_status.side_effect = Exception("connection lost")

        results = reg.check_late_fills(mock_connector)
        assert len(results) == 0

    def test_cleanup_expired(self):
        reg = PendingOrderRegistry(max_age_sec=0.0, load_from_storage=False)
        lc = OrderLifecycle("t1", "agent1", "SiM5", "buy", 10)
        lc.update_from_connector("canceled", filled=5)
        reg.register(lc)

        removed = reg.cleanup_expired()
        assert removed == 1
        assert len(reg.get_pending()) == 0

    def test_multiple_orders_tracked(self):
        reg = PendingOrderRegistry(load_from_storage=False)
        for i in range(5):
            lc = OrderLifecycle(f"t{i}", "agent1", "SiM5", "buy", 10)
            reg.register(lc)
        assert len(reg.get_pending()) == 5

    def test_registry_persists_and_restores_from_storage(self, tmp_path, monkeypatch):
        import core.storage as storage

        monkeypatch.setattr(storage, "PENDING_ORDERS_FILE", tmp_path / "pending_orders.json")

        reg = PendingOrderRegistry(load_from_storage=False)
        lc = OrderLifecycle("t1", "agent1", "SiM5", "buy", 10)
        lc.update_from_connector("working", filled=3, avg_price=85000.0)
        reg.register(lc)

        restored = PendingOrderRegistry()
        restored_pending = [item for item in restored.get_pending() if item.tid == "t1"]

        assert len(restored_pending) == 1
        assert restored_pending[0].tid == "t1"
        assert restored_pending[0].filled_qty == 3

    def test_recover_strategy_orders_marks_missing_broker_state_unresolved(self):
        reg = PendingOrderRegistry(load_from_storage=False)
        lc = OrderLifecycle("t1", "agent1", "SiM5", "buy", 10)
        reg.register(lc)

        mock_connector = MagicMock()
        mock_connector.get_order_status.return_value = None

        result = reg.recover_strategy_orders(mock_connector, "agent1")

        assert result["recovered"] == []
        assert len(result["unresolved"]) == 1
        assert result["unresolved"][0]["reason"] == "missing_on_broker"

    def test_late_fill_detected_after_registry_restore(self, tmp_path, monkeypatch):
        import core.storage as storage

        monkeypatch.setattr(storage, "PENDING_ORDERS_FILE", tmp_path / "pending_orders.json")

        reg = PendingOrderRegistry(load_from_storage=False)
        lc = OrderLifecycle("t1", "agent1", "SiM5", "buy", 10)
        lc.update_from_connector("canceled", filled=5, avg_price=85000.0)
        reg.register(lc)

        restored = PendingOrderRegistry()
        mock_connector = MagicMock()
        mock_connector.get_order_status.return_value = {
            "status": "matched",
            "quantity": 10,
            "balance": 2,
            "avg_price": 85050.0,
        }

        results = restored.check_late_fills(mock_connector)

        assert len(results) == 1
        assert results[0]["delta"] == 3
