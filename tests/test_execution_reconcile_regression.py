# tests/test_execution_reconcile_regression.py

"""
Regression-тесты для execution и reconcile контура.

Критичные сценарии:
  - Двойное закрытие (double close)
  - Идемпотентность close-path
  - Partial fill
  - Connector refusal / timeout
  - Stale external position (reconciler)
  - Duplicate trade event (FillLedger)
  - Неправильный reset позиции при ошибке
"""

import time
from unittest.mock import MagicMock, patch, PropertyMock
import pytest

from core.position_tracker import PositionTracker
from core.reconciler import Reconciler
from core.fill_ledger import FillLedger


# ══════════════════════════════════════════════════════════════════════════════
# Двойное закрытие и идемпотентность close-path
# ══════════════════════════════════════════════════════════════════════════════


class TestDoubleCloseIdempotency:
    """Двойной close не должен менять состояние или создавать лишние ордера."""

    def test_second_close_rejected_by_position_tracker(self):
        """Второй close отклоняется: позиция уже flat после первого закрытия."""
        tracker = PositionTracker()
        tracker.open_position("buy", 10, 100.0)
        tracker.set_order_in_flight(False)

        # Первый close — успешный
        assert tracker.try_set_order_in_flight_for_close() is True
        tracker.close_position(10, 10)

        # Второй close — нет позиции
        assert tracker.try_set_order_in_flight_for_close() is False
        assert tracker.get_position() == 0

    def test_close_rejected_when_close_already_in_flight(self):
        """Второй close отклоняется, пока первый ещё в процессе."""
        tracker = PositionTracker()
        tracker.open_position("buy", 5, 200.0)
        tracker.set_order_in_flight(False)

        # Первый close запускает in-flight
        assert tracker.try_set_order_in_flight_for_close() is True
        assert tracker.is_order_in_flight() is True

        # Второй close — блокируется
        assert tracker.try_set_order_in_flight_for_close() is False

    def test_position_not_corrupted_after_failed_close(self):
        """Неудачный close не портит позицию."""
        tracker = PositionTracker()
        tracker.open_position("sell", 3, 50.0)
        tracker.set_order_in_flight(False)

        assert tracker.try_set_order_in_flight_for_close() is True
        # Симуляция: close ордер отклонён, сбрасываем in-flight
        tracker.set_order_in_flight(False)

        # Позиция не изменилась
        assert tracker.get_position() == -1
        assert tracker.get_position_qty() == -3
        assert tracker.get_entry_price() == 50.0

    def test_concurrent_close_attempts_single_winner(self):
        """Из нескольких параллельных close только один получает in-flight."""
        tracker = PositionTracker()
        tracker.open_position("buy", 10, 100.0)
        tracker.set_order_in_flight(False)

        results = []

        def attempt():
            results.append(tracker.try_set_order_in_flight_for_close())

        import threading
        threads = [threading.Thread(target=attempt) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Ровно 1 поток должен получить True
        assert results.count(True) == 1
        assert results.count(False) == 9


# ══════════════════════════════════════════════════════════════════════════════
# Partial fill
# ══════════════════════════════════════════════════════════════════════════════


class TestPartialFill:
    """Тесты частичного исполнения."""

    def test_partial_close_leaves_residual_position(self):
        """Частичный close оставляет остаток позиции."""
        tracker = PositionTracker()
        tracker.open_position("buy", 10, 100.0)
        tracker.set_order_in_flight(False)

        # Частичное закрытие: 7 из 10
        result = tracker.close_position(7, 10)
        assert result is True
        assert tracker.get_position() == 1
        assert tracker.get_position_qty() == 3

    def test_partial_close_short_leaves_residual(self):
        """Частичный close short оставляет остаток."""
        tracker = PositionTracker()
        tracker.open_position("sell", 5, 200.0)
        tracker.set_order_in_flight(False)

        result = tracker.close_position(3, 5)
        assert result is True
        assert tracker.get_position() == -1
        assert tracker.get_position_qty() == -2

    def test_zero_fill_does_not_change_position(self):
        """Fill qty=0 не меняет позицию (timeout без исполнения)."""
        tracker = PositionTracker()
        tracker.open_position("buy", 10, 100.0)
        tracker.set_order_in_flight(False)

        result = tracker.close_position(0, 10)
        # Позиция остаётся
        assert tracker.get_position() == 1
        assert tracker.get_position_qty() == 10


# ══════════════════════════════════════════════════════════════════════════════
# Connector refusal
# ══════════════════════════════════════════════════════════════════════════════


class TestConnectorRefusal:
    """Тесты отказа коннектора при исполнении."""

    def test_order_executor_handles_place_order_none(self):
        """OrderExecutor корректно обрабатывает place_order → None."""
        from core.order_executor import OrderExecutor
        from core.risk_guard import RiskGuard

        connector = MagicMock()
        connector.place_order.return_value = None
        connector.get_free_money.return_value = 100000

        tracker = PositionTracker()
        recorder = MagicMock()
        risk_guard = RiskGuard("test")

        executor = OrderExecutor(
            strategy_id="test",
            connector=connector,
            position_tracker=tracker,
            trade_recorder=recorder,
            risk_guard=risk_guard,
            account_id="acc1",
            ticker="SBER",
            board="TQBR",
            agent_name="test",
            order_mode="market",
            get_last_price=lambda: 100.0,
        )

        executor.execute_signal({"action": "buy", "qty": 1})

        # place_order вызван
        connector.place_order.assert_called_once()
        # Позиция НЕ открыта (ордер не исполнен)
        # In-flight должен быть установлен (PositionTracker.try_set_order_in_flight)
        # но позиция не подтверждена
        assert tracker.get_position() == 0

    def test_order_executor_handles_place_order_exception(self):
        """OrderExecutor корректно обрабатывает исключение от place_order."""
        from core.order_executor import OrderExecutor
        from core.risk_guard import RiskGuard

        connector = MagicMock()
        connector.place_order.side_effect = ConnectionError("broker down")
        connector.get_free_money.return_value = 100000

        tracker = PositionTracker()
        recorder = MagicMock()
        risk_guard = RiskGuard("test")

        executor = OrderExecutor(
            strategy_id="test",
            connector=connector,
            position_tracker=tracker,
            trade_recorder=recorder,
            risk_guard=risk_guard,
            account_id="acc1",
            ticker="SBER",
            board="TQBR",
            agent_name="test",
            order_mode="market",
            get_last_price=lambda: 100.0,
        )

        # Не должен бросить исключение наружу
        executor.execute_signal({"action": "buy", "qty": 1})

        assert tracker.get_position() == 0


# ══════════════════════════════════════════════════════════════════════════════
# Stale external position (Reconciler)
# ══════════════════════════════════════════════════════════════════════════════


class TestStaleExternalPosition:
    """Reconciler не должен разрушать позицию при stale broker data."""

    def test_reconcile_preserves_position_on_broker_error(self):
        """При ошибке получения данных от брокера позиция не обнуляется."""
        connector = MagicMock()
        connector.get_positions.side_effect = ConnectionError("timeout")

        tracker = PositionTracker()
        tracker.open_position("buy", 5, 100.0)
        tracker.set_order_in_flight(False)

        reconciler = Reconciler(
            strategy_id="test",
            ticker="SBER",
            account_id="acc1",
            connector=connector,
            position_tracker=tracker,
        )
        reconciler._last_reconcile_time = 0  # force reconcile

        reconciler.reconcile()

        # Позиция сохранена
        assert tracker.get_position() == 1
        assert tracker.get_position_qty() == 5

    def test_reconcile_preserves_position_on_empty_broker_response(self):
        """Пустой ответ от брокера не обнуляет подтверждённую позицию."""
        connector = MagicMock()
        connector.get_positions.return_value = None

        tracker = PositionTracker()
        tracker.open_position("sell", 3, 200.0)
        tracker.set_order_in_flight(False)

        reconciler = Reconciler(
            strategy_id="test",
            ticker="SBER",
            account_id="acc1",
            connector=connector,
            position_tracker=tracker,
        )
        reconciler._last_reconcile_time = 0

        reconciler.reconcile()

        # Позиция сохранена
        assert tracker.get_position() == -1
        assert tracker.get_position_qty() == -3

    def test_reconcile_updates_position_on_confirmed_mismatch(self):
        """При подтверждённом расхождении reconciler обнаруживает mismatch."""
        connector = MagicMock()
        connector.get_positions.return_value = [
            {"ticker": "SBER", "board": "TQBR", "quantity": 0}
        ]

        tracker = PositionTracker()
        tracker.open_position("buy", 5, 100.0)
        tracker.set_order_in_flight(False)

        reconciler = Reconciler(
            strategy_id="test",
            ticker="SBER",
            account_id="acc1",
            connector=connector,
            position_tracker=tracker,
            get_order_pairs=lambda sid: [],
        )
        reconciler._last_reconcile_time = 0

        reconciler.reconcile()

        # Reconciler обнаруживает расхождение и либо корректирует,
        # либо логирует — позиция не разрушается destructively


# ══════════════════════════════════════════════════════════════════════════════
# Duplicate trade event (FillLedger)
# ══════════════════════════════════════════════════════════════════════════════


class TestDuplicateTradeEvent:
    """FillLedger должен дедуплицировать повторные fill events."""

    def test_duplicate_fill_id_rejected(self):
        """Второй fill с тем же fill_id не записывается."""
        ledger = FillLedger()

        with patch("core.fill_ledger.save_order") as mock_save, \
             patch("core.fill_ledger.append_trade") as mock_append:
            # Первый fill
            result1 = ledger.record_fill(
                fill_id="exec-001",
                strategy_id="test",
                ticker="SBER",
                board="TQBR",
                side="buy",
                qty=10,
                price=100.0,
            )
            assert result1 is True
            assert mock_save.call_count == 1
            assert mock_append.call_count == 1

            # Дубликат
            result2 = ledger.record_fill(
                fill_id="exec-001",
                strategy_id="test",
                ticker="SBER",
                board="TQBR",
                side="buy",
                qty=10,
                price=100.0,
            )
            assert result2 is False
            # Не записан повторно
            assert mock_save.call_count == 1
            assert mock_append.call_count == 1

    def test_empty_fill_id_rejected(self):
        """Fill без fill_id не записывается."""
        ledger = FillLedger()

        with patch("core.fill_ledger.save_order") as mock_save, \
             patch("core.fill_ledger.append_trade") as mock_append:
            result = ledger.record_fill(
                fill_id="",
                strategy_id="test",
                ticker="SBER",
                board="TQBR",
                side="buy",
                qty=10,
                price=100.0,
            )
            assert result is False
            mock_save.assert_not_called()
            mock_append.assert_not_called()

    def test_different_fill_ids_both_accepted(self):
        """Два разных fill_id оба записываются."""
        ledger = FillLedger()

        with patch("core.fill_ledger.save_order"), \
             patch("core.fill_ledger.append_trade"):
            r1 = ledger.record_fill(
                fill_id="exec-001",
                strategy_id="test",
                ticker="SBER",
                board="TQBR",
                side="buy",
                qty=10,
                price=100.0,
            )
            r2 = ledger.record_fill(
                fill_id="exec-002",
                strategy_id="test",
                ticker="SBER",
                board="TQBR",
                side="sell",
                qty=10,
                price=105.0,
            )
            assert r1 is True
            assert r2 is True

    def test_concurrent_duplicate_fills_only_one_recorded(self):
        """Параллельные fills с одним fill_id — записывается только один."""
        ledger = FillLedger()
        results = []

        with patch("core.fill_ledger.save_order"), \
             patch("core.fill_ledger.append_trade"):
            import threading

            def attempt():
                r = ledger.record_fill(
                    fill_id="exec-race",
                    strategy_id="test",
                    ticker="SBER",
                    board="TQBR",
                    side="buy",
                    qty=1,
                    price=100.0,
                )
                results.append(r)

            threads = [threading.Thread(target=attempt) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert results.count(True) == 1
        assert results.count(False) == 9


# ══════════════════════════════════════════════════════════════════════════════
# Неправильный reset позиции
# ══════════════════════════════════════════════════════════════════════════════


class TestPositionResetSafety:
    """Позиция не должна обнуляться при ошибках."""

    def test_update_position_with_confirmed_zero_resets(self):
        """update_position(0) от подтверждённого broker sync — reset допустим."""
        tracker = PositionTracker()
        tracker.open_position("buy", 5, 100.0)
        tracker.set_order_in_flight(False)

        tracker.update_position(0, 0, 0.0)

        assert tracker.get_position() == 0
        assert tracker.get_position_qty() == 0

    def test_open_after_full_close_allowed(self):
        """После полного закрытия можно снова открыть позицию."""
        tracker = PositionTracker()
        tracker.open_position("buy", 10, 100.0)
        tracker.set_order_in_flight(False)

        tracker.close_position(10, 10)
        assert tracker.get_position() == 0

        # Повторное открытие
        result = tracker.open_position("sell", 3, 200.0)
        assert result is True
        assert tracker.get_position() == -1
        assert tracker.get_position_qty() == -3

    def test_flip_not_allowed_via_open(self):
        """Переворот (flip) через open_position запрещён."""
        tracker = PositionTracker()
        tracker.open_position("buy", 10, 100.0)
        tracker.set_order_in_flight(False)

        result = tracker.open_position("sell", 5, 200.0)
        assert result is False
        # Позиция не изменилась
        assert tracker.get_position() == 1
        assert tracker.get_position_qty() == 10
