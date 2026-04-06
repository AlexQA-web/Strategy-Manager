# tests/test_position_tracker.py

"""Unit-тесты для core/position_tracker.py"""

import threading
import time
import pytest
from core.position_tracker import PositionTracker


class TestPositionTrackerInit:
    """Тесты инициализации PositionTracker."""

    def test_default_state(self):
        """Проверяет начальное состояние трекера."""
        tracker = PositionTracker()
        assert tracker.is_in_position() is False
        assert tracker.get_position() == 0
        assert tracker.get_position_qty() == 0
        assert tracker.get_entry_price() == 0.0
        assert tracker.is_order_in_flight() is False

    def test_get_state_returns_dict(self):
        """Проверяет что get_state возвращает корректный dict."""
        tracker = PositionTracker()
        state = tracker.get_state()
        assert state == {
            "position": 0,
            "position_qty": 0,
            "entry_price": 0.0,
            "order_in_flight": False,
        }


class TestOpenPosition:
    """Тесты открытия позиции."""

    def test_open_long_position(self):
        """Открытие длинной позиции."""
        tracker = PositionTracker()
        result = tracker.open_position("buy", 10, 150.5)
        assert result is True
        assert tracker.is_in_position() is True
        assert tracker.get_position() == 1
        assert tracker.get_position_qty() == 10
        assert tracker.get_entry_price() == 150.5

    def test_open_short_position(self):
        """Открытие короткой позиции."""
        tracker = PositionTracker()
        result = tracker.open_position("sell", 5, 200.0)
        assert result is True
        assert tracker.is_in_position() is True
        assert tracker.get_position() == -1
        assert tracker.get_position_qty() == -5
        assert tracker.get_entry_price() == 200.0

    def test_cannot_open_if_already_in_position(self):
        """Нельзя открыть позицию если уже есть позиция."""
        tracker = PositionTracker()
        tracker.open_position("buy", 10, 150.5)
        result = tracker.open_position("sell", 5, 200.0)
        assert result is False
        # Состояние не изменилось
        assert tracker.get_position() == 1
        assert tracker.get_position_qty() == 10

    def test_cannot_open_if_order_in_flight(self):
        """Нельзя открыть позицию если ордер уже в полёте."""
        tracker = PositionTracker()
        tracker.set_order_in_flight(True)
        result = tracker.open_position("buy", 10, 150.5)
        assert result is False
        assert tracker.is_in_position() is False

    def test_open_sets_order_in_flight(self):
        """open_position устанавливает флаг order_in_flight."""
        tracker = PositionTracker()
        tracker.open_position("buy", 10, 150.5)
        assert tracker.is_order_in_flight() is True


class TestClosePosition:
    """Тесты закрытия позиции."""

    def test_close_position_fully(self):
        """Полное закрытие позиции."""
        tracker = PositionTracker()
        tracker.open_position("buy", 10, 150.5)
        result = tracker.close_position(10, 10)
        assert result is True
        assert tracker.is_in_position() is False
        assert tracker.get_position() == 0
        assert tracker.get_position_qty() == 0
        assert tracker.get_entry_price() == 0.0

    def test_close_position_partially(self):
        """Частичное закрытие позиции."""
        tracker = PositionTracker()
        tracker.open_position("buy", 10, 150.5)
        result = tracker.close_position(4, 10)
        assert result is True
        assert tracker.is_in_position() is True
        assert tracker.get_position() == 1
        assert tracker.get_position_qty() == 6
        assert tracker.get_entry_price() == 150.5

    def test_close_short_position_fully(self):
        """Полное закрытие короткой позиции."""
        tracker = PositionTracker()
        tracker.open_position("sell", 10, 150.5)
        result = tracker.close_position(10, 10)
        assert result is True
        assert tracker.is_in_position() is False
        assert tracker.get_position_qty() == 0

    def test_close_short_position_partially(self):
        """Частичное закрытие короткой позиции."""
        tracker = PositionTracker()
        tracker.open_position("sell", 10, 150.5)
        result = tracker.close_position(3, 10)
        assert result is True
        assert tracker.is_in_position() is True
        assert tracker.get_position() == -1
        assert tracker.get_position_qty() == -7

    def test_close_when_no_position(self):
        """Закрытие когда позиции нет."""
        tracker = PositionTracker()
        result = tracker.close_position(5, 5)
        assert result is False

    def test_close_more_than_position(self):
        """Закрытие большего количества чем есть."""
        tracker = PositionTracker()
        tracker.open_position("buy", 5, 150.5)
        result = tracker.close_position(10, 5)
        assert result is True
        assert tracker.is_in_position() is False


class TestUpdatePosition:
    """Тесты атомарного обновления позиции."""

    def test_update_position(self):
        """Атомарное обновление всех параметров."""
        tracker = PositionTracker()
        tracker.update_position(1, 15, 160.0)
        assert tracker.get_position() == 1
        assert tracker.get_position_qty() == 15
        assert tracker.get_entry_price() == 160.0

    def test_update_position_resets_to_zero(self):
        """Сброс позиции через update."""
        tracker = PositionTracker()
        tracker.open_position("buy", 10, 150.5)
        tracker.update_position(0, 0, 0.0)
        assert tracker.is_in_position() is False
        assert tracker.get_position_qty() == 0
        assert tracker.get_entry_price() == 0.0

    def test_update_position_clears_order_in_flight_on_sync(self):
        """Authoritative sync-path не должен оставлять зависший order_in_flight."""
        tracker = PositionTracker()
        tracker.open_position("buy", 10, 150.5)
        tracker.set_order_in_flight(True)

        tracker.update_position(1, 10, 150.5)

        assert tracker.is_order_in_flight() is False

    def test_sync_position_clears_order_in_flight_when_flattened(self):
        """Явный sync API очищает transient state при reconcile в flat."""
        tracker = PositionTracker()
        tracker.open_position("buy", 10, 150.5)
        tracker.set_order_in_flight(True)

        tracker.sync_position(0, 0, 0.0)

        assert tracker.get_position() == 0
        assert tracker.get_position_qty() == 0
        assert tracker.is_order_in_flight() is False


class TestOrderInFlight:
    """Тесты флага order_in_flight."""

    def test_set_order_in_flight(self):
        """Установка флага."""
        tracker = PositionTracker()
        tracker.set_order_in_flight(True)
        assert tracker.is_order_in_flight() is True

    def test_clear_order_in_flight(self):
        """Сброс флага."""
        tracker = PositionTracker()
        tracker.set_order_in_flight(True)
        tracker.clear_order_in_flight()
        assert tracker.is_order_in_flight() is False


class TestReset:
    """Тесты сброса состояния."""

    def test_reset_clears_all(self):
        """reset() очищает все параметры."""
        tracker = PositionTracker()
        tracker.open_position("buy", 10, 150.5)
        tracker.reset()
        assert tracker.is_in_position() is False
        assert tracker.get_position() == 0
        assert tracker.get_position_qty() == 0
        assert tracker.get_entry_price() == 0.0
        assert tracker.is_order_in_flight() is False


class TestThreadSafety:
    """Тесты потокобезопасности."""

    def test_concurrent_open_position(self):
        """Только один поток может открыть позицию."""
        tracker = PositionTracker()
        results = []

        def try_open(side, qty, price):
            result = tracker.open_position(side, qty, price)
            results.append(result)

        threads = [
            threading.Thread(target=try_open, args=("buy", 10, 150.5)),
            threading.Thread(target=try_open, args=("sell", 5, 200.0)),
            threading.Thread(target=try_open, args=("buy", 8, 160.0)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Только один должен succeed
        assert results.count(True) == 1
        assert results.count(False) == 2

    def test_concurrent_read_write(self):
        """Чтение и запись из разных потоков не вызывают ошибок."""
        tracker = PositionTracker()
        errors = []

        def writer():
            try:
                for i in range(100):
                    tracker.open_position("buy", i + 1, 100.0 + i)
                    tracker.close_position(1, i + 1)
                    tracker.reset()
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(100):
                    tracker.get_state()
                    tracker.is_in_position()
                    tracker.get_position_qty()
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Ошибки в многопоточном тесте: {errors}"


class TestConfirmOpen:
    """Тесты confirm_open — guarded trade-path (TASK-033)."""

    def test_confirm_open_buy_from_flat(self):
        """Открытие long из flat — разрешено."""
        tracker = PositionTracker()
        result = tracker.confirm_open("buy", 10, 150.0)
        assert result is True
        assert tracker.get_position() == 1
        assert tracker.get_position_qty() == 10
        assert tracker.get_entry_price() == 150.0

    def test_confirm_open_sell_from_flat(self):
        """Открытие short из flat — разрешено."""
        tracker = PositionTracker()
        result = tracker.confirm_open("sell", 5, 200.0)
        assert result is True
        assert tracker.get_position() == -1
        assert tracker.get_position_qty() == -5
        assert tracker.get_entry_price() == 200.0

    def test_flip_long_to_short_rejected(self):
        """Flip long→short — запрещён."""
        tracker = PositionTracker()
        tracker.confirm_open("buy", 10, 150.0)
        result = tracker.confirm_open("sell", 5, 200.0)
        assert result is False
        # Позиция не изменилась
        assert tracker.get_position() == 1
        assert tracker.get_position_qty() == 10

    def test_flip_short_to_long_rejected(self):
        """Flip short→long — запрещён."""
        tracker = PositionTracker()
        tracker.confirm_open("sell", 5, 200.0)
        result = tracker.confirm_open("buy", 10, 150.0)
        assert result is False
        assert tracker.get_position() == -1
        assert tracker.get_position_qty() == -5

    def test_scale_in_long_rejected(self):
        """Scale-in long — запрещён."""
        tracker = PositionTracker()
        tracker.confirm_open("buy", 10, 150.0)
        result = tracker.confirm_open("buy", 5, 160.0)
        assert result is False
        assert tracker.get_position_qty() == 10

    def test_scale_in_short_rejected(self):
        """Scale-in short — запрещён."""
        tracker = PositionTracker()
        tracker.confirm_open("sell", 5, 200.0)
        result = tracker.confirm_open("sell", 3, 190.0)
        assert result is False
        assert tracker.get_position_qty() == -5

    def test_reopen_after_close(self):
        """Открытие после полного закрытия — разрешено."""
        tracker = PositionTracker()
        tracker.confirm_open("buy", 10, 150.0)
        tracker.close_position(10, 10)
        result = tracker.confirm_open("sell", 5, 200.0)
        assert result is True
        assert tracker.get_position() == -1
        assert tracker.get_position_qty() == -5

    def test_update_position_not_restricted(self):
        """update_position (sync path) не ограничен матрицей переходов."""
        tracker = PositionTracker()
        tracker.confirm_open("buy", 10, 150.0)
        # Sync path: прямое обновление на short — допустимо
        tracker.update_position(-1, -5, 200.0)
        assert tracker.get_position() == -1
        assert tracker.get_position_qty() == -5
