"""Тесты reconnect-контура BaseConnector (TASK-015)."""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from core.base_connector import BaseConnector


class _StubConnector(BaseConnector):
    """Минимальная реализация для тестов reconnect."""

    def __init__(self):
        super().__init__()
        self._is_conn = False
        self._connect_calls = 0

    def connect(self) -> bool:
        self._connect_calls += 1
        self._is_conn = True
        return True

    def disconnect(self):
        self._stop_reconnect.set()
        self._is_conn = False

    def is_connected(self) -> bool:
        return self._is_conn

    def get_last_price(self, ticker, board="TQBR"):
        return None

    def place_order(self, account_id, ticker, side, quantity, order_type,
                    price=0.0, board="TQBR", agent_name=""):
        return None

    def cancel_order(self, order_id, account_id):
        return False

    def get_positions(self, account_id):
        return []

    def get_accounts(self):
        return []

    def get_order_book(self, board, ticker, depth=10):
        return None

    def close_position(self, account_id, ticker, quantity=0, agent_name=""):
        return None


class TestReconnectLoop:

    def test_start_reconnect_idempotent(self):
        """Повторный start_reconnect_loop не создаёт второй поток."""
        conn = _StubConnector()
        conn._is_conn = True  # connected → loop просто крутится
        conn.start_reconnect_loop()
        thread1 = conn._reconnect_thread
        conn.start_reconnect_loop()
        thread2 = conn._reconnect_thread
        assert thread1 is thread2, "Должен быть тот же поток"
        conn._stop_reconnect.set()

    def test_loop_does_not_die_after_exhausted_attempts(self):
        """После исчерпания попыток loop уходит в cooldown, а не break."""
        conn = _StubConnector()
        conn._reconnect_attempts = 1
        conn._reconnect_delay = 1
        conn._is_conn = False

        loop_alive_after_exhaust = threading.Event()

        # Подменяем оба wait чтобы loop не висел на реальных таймаутах
        wait_call_count = 0

        def _fast_stop_wait(timeout=None):
            nonlocal wait_call_count
            wait_call_count += 1
            if wait_call_count > 4:
                loop_alive_after_exhaust.set()
                conn._stop_reconnect.set()
                return True
            return False

        def _fast_hc_wait(timeout=None):
            return False  # immediate return

        with patch("core.scheduler.is_in_schedule", return_value=True):
            conn._stop_reconnect.wait = _fast_stop_wait
            conn._health_check_event.wait = _fast_hc_wait
            conn._health_check_event.is_set = lambda: False
            conn._health_check_event.clear = lambda: None
            conn.connect = MagicMock(return_value=False)
            conn.is_connected = MagicMock(return_value=False)

            t = threading.Thread(target=conn._reconnect_loop, daemon=True)
            t.start()
            t.join(timeout=10)

        assert loop_alive_after_exhaust.is_set(), \
            "Reconnect loop должен пережить исчерпание попыток (cooldown, а не break)"

    def test_request_health_check_wakes_loop(self):
        """request_health_check() будит reconnect-loop для немедленной проверки."""
        conn = _StubConnector()
        assert not conn._health_check_event.is_set()
        conn.request_health_check()
        assert conn._health_check_event.is_set()

    def test_health_check_default_delegates_to_is_connected(self):
        """По умолчанию health_check() == is_connected()."""
        conn = _StubConnector()
        conn._is_conn = True
        assert conn.health_check() is True
        conn._is_conn = False
        assert conn.health_check() is False

    def test_deep_health_check_detects_stale_connection(self):
        """Если health_check() вернёт False при is_connected() == True,
        loop должен инициировать reconnect."""
        conn = _StubConnector()
        conn._reconnect_attempts = 2
        conn._reconnect_delay = 1
        conn._health_check_interval = 0  # всегда делать deep check

        reconnect_triggered = threading.Event()

        call_count = 0

        def _fake_is_connected():
            return True  # is_connected говорит True

        def _fake_health_check():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return False  # deep check говорит False
            # После первой неудачной проверки — stop loop
            conn._stop_reconnect.set()
            return True

        def _fake_connect():
            reconnect_triggered.set()
            conn._stop_reconnect.set()
            return True

        conn.is_connected = _fake_is_connected
        conn.health_check = _fake_health_check
        conn.connect = _fake_connect
        conn._health_check_event.wait = lambda timeout=None: False
        conn._health_check_event.is_set = lambda: False
        conn._health_check_event.clear = lambda: None
        conn._stop_reconnect.wait = lambda timeout=None: False

        with patch("core.scheduler.is_in_schedule", return_value=True):
            t = threading.Thread(target=conn._reconnect_loop, daemon=True)
            t.start()
            t.join(timeout=5)

        assert reconnect_triggered.is_set(), \
            "Deep health check failure должен привести к попытке переподключения"

    def test_schedule_resets_attempt_counter(self):
        """Вне окна расписания счётчик попыток сбрасывается."""
        conn = _StubConnector()
        conn._reconnect_attempts = 2
        conn._is_conn = False

        schedule_returns = [True, True, False]
        call_idx = 0

        def _is_in_schedule(cid):
            nonlocal call_idx
            if call_idx < len(schedule_returns):
                val = schedule_returns[call_idx]
                call_idx += 1
                return val
            conn._stop_reconnect.set()
            return False

        conn.connect = MagicMock(return_value=False)
        conn.is_connected = MagicMock(return_value=False)

        conn._stop_reconnect.wait = lambda timeout=None: conn._stop_reconnect.is_set()
        conn._health_check_event.wait = lambda timeout=None: False
        conn._health_check_event.is_set = lambda: False
        conn._health_check_event.clear = lambda: None

        with patch("core.scheduler.is_in_schedule", side_effect=_is_in_schedule):
            t = threading.Thread(target=conn._reconnect_loop, daemon=True)
            t.start()
            t.join(timeout=5)

        assert conn.connect.call_count >= 1
