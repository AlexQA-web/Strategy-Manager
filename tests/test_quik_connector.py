import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.base_connector import OrderOutcome
from core.quik_connector import QuikConnector


def test_connect_success_initializes_client_and_reconnect(monkeypatch):
    class FakeQuikPy:
        def __init__(self, **kwargs):
            self.accounts = []

        def is_connected(self):
            return {"data": 1}

        def close_connection_and_thread(self):
            return None

    monkeypatch.setitem(sys.modules, "QuikPy", SimpleNamespace(QuikPy=FakeQuikPy))
    monkeypatch.setattr("core.quik_connector.get_setting", lambda key: None)
    connector = QuikConnector()
    connector._is_broker_online = lambda: True
    connector.start_reconnect_loop = MagicMock()
    connector._sync_orders_cache = MagicMock()
    connector._fire_event = MagicMock()

    assert connector.connect() is True
    assert connector.is_connected() is True
    connector.start_reconnect_loop.assert_called_once()
    connector._sync_orders_cache.assert_called_once()


def test_connect_connection_refused_returns_false(monkeypatch):
    class FakeQuikPy:
        def __init__(self, **kwargs):
            raise ConnectionRefusedError("down")

    monkeypatch.setitem(sys.modules, "QuikPy", SimpleNamespace(QuikPy=FakeQuikPy))
    monkeypatch.setattr("core.quik_connector.get_setting", lambda key: None)
    connector = QuikConnector()
    connector._fire_event = MagicMock()

    assert connector.connect() is False


def test_place_order_result_validates_and_submits():
    connector = QuikConnector()
    connector._connected = True
    connector._client = MagicMock()
    connector._client.send_transaction.return_value = {"data": "12345"}

    result = connector.place_order_result(
        account_id="ACC1",
        ticker="SBER",
        side="buy",
        quantity=2,
        order_type="market",
        board="TQBR",
    )

    assert result.outcome == OrderOutcome.SUCCESS
    assert result.transaction_id == "12345"


def test_get_positions_combines_futures_and_stock_data():
    connector = QuikConnector()
    connector._connected = True
    connector._client = MagicMock()
    connector._client.accounts = [{"client_code": "ACC1", "trade_account_id": "TRD1", "firm_id": "SPBFUT"}]
    connector._client.get_futures_holdings.return_value = {
        "data": [{"trdaccid": "TRD1", "sec_code": "SiH6", "class_code": "FUT", "totalnet": 2, "avrposnprice": 100.0}]
    }
    connector._client.get_portfolio_info_ex.return_value = {
        "data": [{"sec_code": "SBER", "class_code": "TQBR", "currentbal": 3, "awg_position_price": 250.0}]
    }

    positions = connector.get_positions("ACC1")

    assert {item["ticker"] for item in positions} == {"SiH6", "SBER"}


def test_get_order_status_maps_terminal_state_from_orders_cache():
    connector = QuikConnector()
    connector._connected = True
    connector._client = MagicMock()
    connector._client.get_all_orders.return_value = {
        "data": [{"trans_id": "77", "qty": 5, "balance": 0, "status": "3"}]
    }

    status = connector.get_order_status("77")

    assert status == {"status": "matched", "quantity": 5, "balance": 0}


def test_get_history_returns_dataframe(monkeypatch):
    connector = QuikConnector()
    connector._connected = True
    connector._client = MagicMock()
    connector._client.get_candles_from_data_source.return_value = {
        "data": [
            {
                "datetime": {"year": 2024, "month": 1, "day": 1, "hour": 10, "min": 0, "sec": 0},
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
                "volume": 10,
            }
        ]
    }

    df = connector.get_history("SBER", "TQBR", "1d", 3650)

    assert df is not None
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]