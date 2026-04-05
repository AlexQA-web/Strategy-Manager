# tests/test_finam_free_money.py

"""
Тесты get_free_money в FinamConnector (TASK-038).

Проверяем:
  1. Точное совпадение по client_id возвращает корректное значение.
  2. Резолв через forts_acc работает.
  3. При неоднозначном маппинге возвращается None (не fallback).
  4. Пустой _client_limits → None.
"""

from unittest.mock import patch

import pytest


@pytest.fixture
def connector():
    """Создаёт FinamConnector без загрузки DLL."""
    with patch("core.finam_connector.MOEXClient"):
        from core.finam_connector import FinamConnector
        c = FinamConnector()
    return c


class TestGetFreeMoney:

    def test_exact_client_id_match(self, connector):
        """Точное совпадение по client_id → money_free."""
        connector._client_limits = {
            "C1": {"money_free": 50000.0},
            "C2": {"money_free": 80000.0},
        }
        assert connector.get_free_money("C1") == 50000.0
        assert connector.get_free_money("C2") == 80000.0

    def test_forts_acc_resolution(self, connector):
        """account_id == forts_acc → резолвится через accounts."""
        connector._accounts = [
            {
                "id": "U1",
                "sub_accounts": [
                    {"client_id": "C1", "forts_acc": "F001"}
                ],
            }
        ]
        connector._client_limits = {"C1": {"money_free": 42000.0}}
        assert connector.get_free_money("F001") == 42000.0

    def test_unknown_account_returns_none(self, connector):
        """Неизвестный account_id → None, без fallback."""
        connector._client_limits = {
            "C1": {"money_free": 50000.0},
            "C2": {"money_free": 80000.0},
        }
        connector._accounts = []
        result = connector.get_free_money("UNKNOWN")
        assert result is None

    def test_no_fallback_to_first_nonzero(self, connector):
        """Ранее был fallback на первый ненулевой — теперь None."""
        connector._client_limits = {
            "C1": {"money_free": 100000.0},
        }
        connector._accounts = []
        # "C1" не совпадает с "OTHER", fallback убран
        result = connector.get_free_money("OTHER")
        assert result is None

    def test_no_substring_match(self, connector):
        """Ранее был поиск по подстроке — теперь убран."""
        connector._client_limits = {
            "CLIENT_123": {"money_free": 77000.0},
        }
        connector._accounts = []
        # "123" — подстрока "CLIENT_123", но не должно матчить
        result = connector.get_free_money("123")
        assert result is None

    def test_empty_client_limits(self, connector):
        """Пустой _client_limits → None."""
        connector._client_limits = {}
        connector._accounts = []
        assert connector.get_free_money("C1") is None

    def test_money_free_is_none_in_limits(self, connector):
        """money_free отсутствует в лимитах → None."""
        connector._client_limits = {"C1": {"money_current": 50000.0}}
        assert connector.get_free_money("C1") is None

    def test_multi_account_isolation(self, connector):
        """Каждый account_id получает свои средства, не чужие."""
        connector._client_limits = {
            "ACC_A": {"money_free": 10000.0},
            "ACC_B": {"money_free": 99000.0},
        }
        connector._accounts = []
        assert connector.get_free_money("ACC_A") == 10000.0
        assert connector.get_free_money("ACC_B") == 99000.0
        assert connector.get_free_money("ACC_C") is None
