"""Contract tests: QuikConnector adapter vs QuikPy vendor layer.

Проверяет, что QuikPy содержит все методы, которые вызывает adapter,
с ожидаемыми сигнатурами. Ловит source/runtime drift до боевой сессии.
"""

import inspect
import importlib
import pytest


@pytest.fixture(scope="module")
def quikpy_class():
    """Импортирует QuikPy и возвращает класс."""
    mod = importlib.import_module("QuikPy")
    cls = getattr(mod, "QuikPy", None)
    if cls is None:
        pytest.skip("QuikPy class not found in QuikPy module")
    return cls


# Методы, которые adapter вызывает, и минимальные ожидаемые параметры
# (без self и trans_id, которые есть у всех).
EXPECTED_METHODS = {
    "is_connected":              [],
    "get_info_param":            ["params"],
    "send_transaction":          ["transaction"],
    "get_all_orders":            [],
    "get_money_limits":          [],
    "get_client_codes":          [],
    "get_classes_list":          [],
    "get_class_securities":      ["class_code"],
    "get_futures_holdings":      [],
    "get_portfolio_info_ex":     ["firm_id", "client_code", "limit_kind"],
    "get_param_ex":              ["class_code", "sec_code", "param_name"],
    "get_candles_from_data_source": ["class_code", "sec_code", "interval"],
    "get_quote_level2":          ["class_code", "sec_code"],
    "get_all_depo_limits":       [],
    "get_trade_accounts":        [],
}


class TestQuikPyContract:
    """Проверяет наличие и сигнатуры методов QuikPy, используемых адаптером."""

    @pytest.mark.parametrize("method_name,expected_params", list(EXPECTED_METHODS.items()))
    def test_method_exists_with_params(self, quikpy_class, method_name, expected_params):
        method = getattr(quikpy_class, method_name, None)
        assert method is not None, (
            f"QuikPy.{method_name}() not found — adapter calls this method"
        )
        sig = inspect.signature(method)
        param_names = [
            p for p in sig.parameters
            if p not in ("self", "trans_id")
        ]
        for expected in expected_params:
            assert expected in param_names, (
                f"QuikPy.{method_name}() missing parameter '{expected}', "
                f"has: {param_names}"
            )
