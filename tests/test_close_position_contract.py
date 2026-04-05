# tests/test_close_position_contract.py

"""Contract tests для BaseConnector.close_position().

Оба коннектора (Finam, QUIK) должны вести себя одинаково
относительно каноники, описанной в BaseConnector.close_position docstring:
  - quantity=0 → закрыть всю позицию
  - 0 < quantity <= abs(pos) → закрыть указанное количество
  - quantity > abs(pos) → закрыть всю позицию (clamp)
  - нет позиции → вернуть None, не вызывать place_order
  - нулевая позиция → вернуть None
  - long позиция → side="sell"
  - short позиция → side="buy"
"""

from unittest.mock import MagicMock, patch
import pytest

from core.finam_connector import FinamConnector
from core.quik_connector import QuikConnector


# ── Helpers ─────────────────────────────────────────────────────────────

def _make_finam(positions: list[dict], place_order_return="tid_123"):
    """Создаёт FinamConnector с подменённым get_positions и place_order."""
    connector = FinamConnector.__new__(FinamConnector)
    connector.get_positions = MagicMock(return_value=positions)
    connector.place_order = MagicMock(return_value=place_order_return)
    return connector


def _make_quik(positions: list[dict], place_order_return="tid_123"):
    """Создаёт QuikConnector с подменённым get_positions и place_order."""
    connector = QuikConnector.__new__(QuikConnector)
    connector.get_positions = MagicMock(return_value=positions)
    connector.place_order = MagicMock(return_value=place_order_return)
    return connector


# ── Parametrized contract matrix ────────────────────────────────────────

_LONG_POS = [{"ticker": "SBER", "board": "TQBR", "quantity": 10}]
_SHORT_POS = [{"ticker": "SBER", "board": "TQBR", "quantity": -5}]
_ZERO_POS = [{"ticker": "SBER", "board": "TQBR", "quantity": 0}]
_NO_POS: list[dict] = []


@pytest.fixture(params=["finam", "quik"], ids=["Finam", "QUIK"])
def make_connector(request):
    """Фабрика коннектора: finam или quik."""
    if request.param == "finam":
        return _make_finam
    return _make_quik


class TestClosePositionContract:
    """Contract tests: одинаковое поведение Finam и QUIK."""

    def test_full_close_long(self, make_connector):
        """quantity=0 на long → sell всю позицию."""
        c = make_connector(_LONG_POS)
        tid = c.close_position("acc", "SBER", quantity=0)

        assert tid == "tid_123"
        c.place_order.assert_called_once()
        kw = c.place_order.call_args.kwargs
        assert kw["side"] == "sell"
        assert kw["quantity"] == 10

    def test_full_close_short(self, make_connector):
        """quantity=0 на short → buy всю позицию."""
        c = make_connector(_SHORT_POS)
        tid = c.close_position("acc", "SBER", quantity=0)

        assert tid == "tid_123"
        kw = c.place_order.call_args.kwargs
        assert kw["side"] == "buy"
        assert kw["quantity"] == 5

    def test_partial_close(self, make_connector):
        """0 < quantity < abs(pos) → частичное закрытие."""
        c = make_connector(_LONG_POS)
        tid = c.close_position("acc", "SBER", quantity=3)

        assert tid == "tid_123"
        kw = c.place_order.call_args.kwargs
        assert kw["quantity"] == 3
        assert kw["side"] == "sell"

    def test_close_exact_total(self, make_connector):
        """quantity == abs(pos) → закрыть ровно всю."""
        c = make_connector(_LONG_POS)
        tid = c.close_position("acc", "SBER", quantity=10)

        assert tid == "tid_123"
        kw = c.place_order.call_args.kwargs
        assert kw["quantity"] == 10

    def test_close_over_total_clamps(self, make_connector):
        """quantity > abs(pos) → clamp до abs(pos)."""
        c = make_connector(_LONG_POS)
        tid = c.close_position("acc", "SBER", quantity=99)

        assert tid == "tid_123"
        kw = c.place_order.call_args.kwargs
        assert kw["quantity"] == 10

    def test_no_position_returns_none(self, make_connector):
        """Тикер не найден → None, place_order не вызван."""
        c = make_connector(_NO_POS)
        tid = c.close_position("acc", "SBER", quantity=0)

        assert tid is None
        c.place_order.assert_not_called()

    def test_zero_position_returns_none(self, make_connector):
        """Позиция = 0 → None, place_order не вызван."""
        c = make_connector(_ZERO_POS)
        tid = c.close_position("acc", "SBER", quantity=0)

        assert tid is None
        c.place_order.assert_not_called()

    def test_board_passed_through(self, make_connector):
        """board из позиции прокидывается в place_order."""
        pos = [{"ticker": "SiM5", "board": "SPBFUT", "quantity": 2}]
        c = make_connector(pos)
        c.close_position("acc", "SiM5")

        kw = c.place_order.call_args.kwargs
        assert kw["board"] == "SPBFUT"

    def test_agent_name_passed(self, make_connector):
        """agent_name передаётся в place_order."""
        c = make_connector(_LONG_POS)
        c.close_position("acc", "SBER", agent_name="my_strategy")

        kw = c.place_order.call_args.kwargs
        assert kw["agent_name"] == "my_strategy"

    def test_order_type_is_market(self, make_connector):
        """Закрытие всегда рыночное."""
        c = make_connector(_LONG_POS)
        c.close_position("acc", "SBER")

        kw = c.place_order.call_args.kwargs
        assert kw["order_type"] == "market"

    def test_place_order_failure_returns_none(self, make_connector):
        """place_order вернул None → close_position тоже None."""
        c = make_connector(_LONG_POS, place_order_return=None)
        tid = c.close_position("acc", "SBER")

        assert tid is None


class TestExecutorCloseContract:
    """Проверяет, что OrderExecutor._execute_market_close вызывает
    connector.close_position с quantity (а не без)."""

    def test_close_passes_quantity(self):
        from core.order_executor import OrderExecutor
        from core.position_tracker import PositionTracker

        connector = MagicMock()
        connector.close_position.return_value = "tid_close"
        pt = PositionTracker()
        pt.open_position("buy", 5, 100.0)

        executor = OrderExecutor(
            strategy_id="test",
            connector=connector,
            position_tracker=pt,
            trade_recorder=MagicMock(),
            risk_guard=MagicMock(),
            account_id="acc",
            ticker="SBER",
            board="TQBR",
            agent_name="agent",
        )

        executor._execute_market_close("sell", 5, "test", 100.0)

        connector.close_position.assert_called_once_with(
            account_id="acc",
            ticker="SBER",
            quantity=5,
            agent_name="agent",
        )

    def test_close_fallback_to_place_order(self):
        """close_position вернул None → fallback на place_order."""
        from core.order_executor import OrderExecutor
        from core.position_tracker import PositionTracker

        connector = MagicMock()
        connector.close_position.return_value = None
        connector.place_order.return_value = "tid_fb"
        pt = PositionTracker()

        executor = OrderExecutor(
            strategy_id="test",
            connector=connector,
            position_tracker=pt,
            trade_recorder=MagicMock(),
            risk_guard=MagicMock(),
            account_id="acc",
            ticker="SBER",
            board="TQBR",
            agent_name="agent",
        )

        executor._execute_market_close("sell", 5, "test", 100.0)

        connector.place_order.assert_called_once()
        kw = connector.place_order.call_args.kwargs
        assert kw["side"] == "sell"
        assert kw["quantity"] == 5
        assert kw["order_type"] == "market"
