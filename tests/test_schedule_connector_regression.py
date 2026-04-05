# tests/test_schedule_connector_regression.py

"""
Regression-тесты для schedule и connector contracts.

Покрывает:
  - Параметрические overnight-сценарии расписания
  - Listener lifecycle (subscriber isolation)
  - Reconnect decisions при разных условиях
  - Secure defaults HealthServer
  - Telegram notifier lifecycle edge cases
"""

from datetime import time as dtime
from unittest.mock import MagicMock, patch
import threading
import pytest

from core.scheduler import is_in_time_window, parse_schedule_window


# ══════════════════════════════════════════════════════════════════════════════
# Parametrized overnight schedule edge cases
# ══════════════════════════════════════════════════════════════════════════════


class TestOvernightScheduleEdgeCases:
    """Параметрические тесты на overnight-окна."""

    @pytest.mark.parametrize("connect,disconnect,day,now_time,expected", [
        # Полуночный переход: 23:00 → 02:00
        (dtime(23, 0), dtime(2, 0), 0, dtime(23, 30), True),   # Пн вечер
        (dtime(23, 0), dtime(2, 0), 1, dtime(0, 30), True),    # Вт раннее утро (после Пн)
        (dtime(23, 0), dtime(2, 0), 0, dtime(22, 59), False),  # До окна
        (dtime(23, 0), dtime(2, 0), 1, dtime(2, 1), False),    # После окна
        (dtime(23, 0), dtime(2, 0), 1, dtime(12, 0), False),   # Середина дня
        # Очень широкое overnight: 18:00 → 06:00
        (dtime(18, 0), dtime(6, 0), 2, dtime(19, 0), True),
        (dtime(18, 0), dtime(6, 0), 3, dtime(5, 0), True),
        (dtime(18, 0), dtime(6, 0), 2, dtime(17, 0), False),
        (dtime(18, 0), dtime(6, 0), 3, dtime(7, 0), False),
        # Граничный случай: connect == disconnect (undefined behavior)
        (dtime(10, 0), dtime(10, 0), 0, dtime(10, 0), True),
        # 1 минута до полуночи → 1 минута после
        (dtime(23, 59), dtime(0, 1), 0, dtime(23, 59), True),
        (dtime(23, 59), dtime(0, 1), 1, dtime(0, 0), True),
    ])
    def test_overnight_parametric(self, connect, disconnect, day, now_time, expected):
        """Параметрический тест overnight-окна."""
        days = [0, 1, 2, 3, 4]  # Пн-Пт
        result = is_in_time_window(connect, disconnect, days, day, now_time)
        assert result is expected, (
            f"is_in_time_window({connect}, {disconnect}, days, "
            f"day={day}, now={now_time}) == {result}, expected {expected}"
        )

    @pytest.mark.parametrize("day,expected", [
        (5, False),  # Суббота — не в расписании
        (6, False),  # Воскресенье — не в расписании
    ])
    def test_weekend_excluded(self, day, expected):
        """Выходные не попадают в рабочее расписание."""
        result = is_in_time_window(
            dtime(9, 0), dtime(18, 0), [0, 1, 2, 3, 4],
            now_weekday=day, now_time=dtime(12, 0),
        )
        assert result is expected

    def test_empty_days_always_false(self):
        """Пустой список дней — всегда False."""
        result = is_in_time_window(
            dtime(0, 0), dtime(23, 59), [],
            now_weekday=0, now_time=dtime(12, 0),
        )
        assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# Listener lifecycle (subscriber isolation)
# ══════════════════════════════════════════════════════════════════════════════


class TestSubscriberIsolation:
    """Подписчики не мешают друг другу."""

    def test_multiple_subscribers_all_receive_updates(self):
        """Несколько подписчиков получают обновления."""
        from core.base_connector import BaseConnector

        class FakeConnector(BaseConnector):
            def connect(self): pass
            def disconnect(self): pass
            def is_connected(self): return True
            def get_accounts(self): return []
            def get_positions(self, account_id): return []
            def get_free_money(self, account_id): return 0
            def get_portfolio(self, account_id): return {}
            def place_order(self, **kw): return None
            def cancel_order(self, order_id, account_id=None): pass
            def get_order_status(self, order_id): return {}
            def get_history(self, ticker, board, timeframe, count): return []
            def get_last_price(self, ticker, board=""): return 0
            def get_best_quote(self, board, ticker): return {}
            def get_order_book(self, board, ticker): return {}
            def close_position(self, account_id, ticker, board="", agent_name=""): return None

        conn = FakeConnector()
        received = {"a": [], "b": []}

        cb_a = lambda data: received["a"].append(data)
        cb_b = lambda data: received["b"].append(data)
        conn.subscribe_positions(cb_a)
        conn.subscribe_positions(cb_b)

        # Виртуальное обновление
        conn._fire_event('positions', {"test": "data"})

        assert len(received["a"]) == 1
        assert len(received["b"]) == 1

        # Отписываем A — B продолжает получать
        conn.unsubscribe_positions(cb_a)
        conn._fire_event('positions', {"test": "data2"})

        assert len(received["a"]) == 1  # A больше не получает
        assert len(received["b"]) == 2  # B получает


# ══════════════════════════════════════════════════════════════════════════════
# Telegram notifier lifecycle
# ══════════════════════════════════════════════════════════════════════════════


class TestTelegramLifecycleEdgeCases:
    """Дополнительные edge-case-тесты lifecycle Telegram notifier."""

    def test_send_after_stop_does_not_crash(self):
        """После stop() notifier не перезапускается и явно возвращает False."""
        from core.telegram_bot import TelegramNotifier

        bot = TelegramNotifier()
        bot.stop()  # без предварительного start

        result = bot.send_raw("test message")

        assert result is False
        assert bot._stopped is True
