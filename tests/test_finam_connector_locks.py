"""
Тесты lock-дисциплины FinamConnector (TASK-011).

Проверяем:
  1. Публичные геттеры возвращают copy-on-read (мутация снаружи не портит внутреннее состояние).
  2. _state_lock защищает _connected, _securities, _positions, _accounts.
  3. _throttle_lock защищает _error_throttle, _sec_info_failures.
  4. _last_order_cleanup обновляется внутри _order_status_lock.
"""
import threading
import time
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def connector():
    """Создаёт FinamConnector без загрузки DLL."""
    with patch("core.finam_connector.MOEXClient"):
        from core.finam_connector import FinamConnector
        c = FinamConnector()
    return c


class TestCopyOnRead:
    """Публичные геттеры возвращают snapshot — мутация не влияет на внутреннее состояние."""

    def test_get_positions_returns_copy(self, connector):
        connector._positions = [{"ticker": "SBER", "quantity": 10}]
        result = connector.get_positions("acc1")
        result.append({"ticker": "GAZP", "quantity": 5})
        assert len(connector._positions) == 1

    def test_get_positions_dict_copy(self, connector):
        connector._positions = [{"ticker": "SBER", "quantity": 10}]
        result = connector.get_positions("acc1")
        result[0]["quantity"] = 999
        assert connector._positions[0]["quantity"] == 10

    def test_get_accounts_returns_copy(self, connector):
        connector._accounts = [{"id": "U1", "name": "Union1"}]
        result = connector.get_accounts()
        result.append({"id": "U2"})
        assert len(connector._accounts) == 1

    def test_get_securities_returns_copy(self, connector):
        connector._securities = [{"ticker": "SBER", "board": "TQBR"}]
        result = connector.get_securities()
        result.append({"ticker": "FAKE"})
        assert len(connector._securities) == 1

    def test_get_securities_filtered_returns_copy(self, connector):
        connector._securities = [
            {"ticker": "SBER", "board": "TQBR"},
            {"ticker": "SiM5", "board": "FUT"},
        ]
        result = connector.get_securities("TQBR")
        assert len(result) == 1
        result[0]["ticker"] = "MUTATED"
        assert connector._securities[0]["ticker"] == "SBER"

    def test_get_all_positions_returns_copy(self, connector):
        connector._accounts = [{"id": "U1"}]
        connector._positions = [{"ticker": "SBER", "quantity": 1}]
        result = connector.get_all_positions()
        result["U1"].append({"ticker": "FAKE"})
        assert len(connector._positions) == 1

    def test_get_order_status_returns_copy(self, connector):
        with connector._order_status_lock:
            connector._order_status["tid-1"] = {"status": "working", "balance": 1}
        result = connector.get_order_status("tid-1")
        result["status"] = "mutated"
        with connector._order_status_lock:
            assert connector._order_status["tid-1"]["status"] == "working"

    def test_get_sec_info_returns_copy(self, connector):
        with connector._sec_info_lock:
            connector._sec_info["SBER"] = {"ticker": "SBER", "lotsize": 10}
        connector._connected = True
        result = connector.get_sec_info("SBER")
        result["lotsize"] = 999
        with connector._sec_info_lock:
            assert connector._sec_info["SBER"]["lotsize"] == 10


class TestStateLock:
    """_state_lock защищает _connected, _securities, _positions, _accounts."""

    def test_is_connected_under_lock(self, connector):
        connector._connected = True
        assert connector.is_connected() is True
        connector._connected = False
        assert connector.is_connected() is False

    def test_parse_positions_writes_under_lock(self, connector):
        """_parse_positions присваивает _positions под _state_lock."""
        import xml.etree.ElementTree as ET
        xml_str = (
            '<positions>'
            '<forts_position><seccode>SiM5</seccode><totalnet>2</totalnet>'
            '<openavgprice>100.0</openavgprice><varmargin>0</varmargin></forts_position>'
            '</positions>'
        )
        root = ET.fromstring(xml_str)
        connector._parse_positions(root)
        positions = connector.get_positions("any")
        assert len(positions) == 1
        assert positions[0]["ticker"] == "SiM5"

    def test_parse_client_writes_under_lock(self, connector):
        """_parse_client добавляет account под _state_lock."""
        import xml.etree.ElementTree as ET
        xml_str = (
            '<client id="C1">'
            '<union>U1</union><market>4</market><type>futures</type>'
            '<currency>RUB</currency><forts_acc>F1</forts_acc>'
            '</client>'
        )
        root = ET.fromstring(xml_str)
        connector._parse_client(root)
        accounts = connector.get_accounts()
        assert len(accounts) == 1
        assert accounts[0]["id"] == "U1"

    def test_concurrent_is_connected_reads_do_not_crash(self, connector):
        stop_event = threading.Event()
        errors = []

        def writer():
            for idx in range(200):
                with connector._state_lock:
                    connector._connected = bool(idx % 2)
                time.sleep(0.001)
            stop_event.set()

        def reader():
            while not stop_event.is_set():
                try:
                    connector.is_connected()
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for thread in threads:
            thread.start()
        writer_thread = threading.Thread(target=writer)
        writer_thread.start()
        writer_thread.join(timeout=5)
        for thread in threads:
            thread.join(timeout=5)

        assert errors == []

    def test_concurrent_get_all_positions_snapshot(self, connector):
        stop_event = threading.Event()
        errors = []

        def writer():
            for idx in range(100):
                with connector._state_lock:
                    connector._accounts = [{"id": f"U{idx}"}]
                    connector._positions = [{"ticker": "SBER", "quantity": idx}]
                time.sleep(0.001)
            stop_event.set()

        def reader():
            while not stop_event.is_set():
                try:
                    snapshot = connector.get_all_positions()
                    for positions in snapshot.values():
                        assert isinstance(positions, list)
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for thread in threads:
            thread.start()
        writer_thread = threading.Thread(target=writer)
        writer_thread.start()
        writer_thread.join(timeout=5)
        for thread in threads:
            thread.join(timeout=5)

        assert errors == []


class TestThrottleLock:
    """_throttle_lock защищает _error_throttle и _sec_info_failures."""

    def test_remember_and_check_sec_info_failure(self, connector):
        connector._remember_sec_info_failure("SBER", "TQBR")
        assert connector._has_recent_sec_info_failure("SBER", "TQBR") is True

    def test_clear_sec_info_failure(self, connector):
        connector._remember_sec_info_failure("SBER", "TQBR")
        connector._clear_sec_info_failure("SBER", "TQBR")
        assert connector._has_recent_sec_info_failure("SBER", "TQBR") is False

    def test_should_emit_error_throttles(self, connector):
        assert connector._should_emit_error("test error") is True
        assert connector._should_emit_error("test error") is False

    def test_should_emit_error_different_messages(self, connector):
        assert connector._should_emit_error("error A") is True
        assert connector._should_emit_error("error B") is True


class TestOrderCleanupLock:
    """_last_order_cleanup обновляется внутри _order_status_lock."""

    def test_cleanup_updates_timestamp_inside_lock(self, connector):
        now = time.time()
        connector._last_order_cleanup = now - 7200  # 2 часа назад
        # Добавляем старый ордер
        with connector._order_status_lock:
            connector._order_status["T1"] = {"status": "matched"}
            connector._order_status_timestamps["T1"] = now - 7200
        connector._cleanup_old_order_status(now)
        # _last_order_cleanup должен быть обновлён
        assert connector._last_order_cleanup == now
        # Ордер должен быть удалён
        assert "T1" not in connector._order_status
