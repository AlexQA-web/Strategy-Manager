"""Regression-тесты для strategy-owned position book и strategy flatten."""

from unittest.mock import MagicMock, patch

import pytest

from core.base_connector import OrderOutcome, OrderResult
from core.order_history import clear_orders, make_order, save_order
from core.position_manager import PositionManager
from core.strategy_flatten import StrategyFlattenExecutor, build_strategy_flatten_plan
from core.strategy_position_book import get_strategy_position, get_strategy_position_book


SID_1 = "strategy_one"
SID_2 = "strategy_two"


def _bind_result_api(connector):
    def _close_position_result(**call_kwargs):
        value = connector.close_position(**call_kwargs)
        if value:
            transaction_id = value if isinstance(value, str) else "legacy-close"
            return OrderResult(OrderOutcome.SUCCESS, transaction_id=transaction_id)
        return OrderResult(OrderOutcome.REJECTED, message="mock_close_position_none")

    connector.close_position_result.side_effect = _close_position_result
    return connector


@pytest.fixture(autouse=True)
def _clear_strategy_orders():
    clear_orders(SID_1)
    clear_orders(SID_2)
    yield
    clear_orders(SID_1)
    clear_orders(SID_2)


def _save(strategy_id: str, side: str, qty: int, price: float, ticker: str = "SBER", board: str = "TQBR"):
    order = make_order(
        strategy_id=strategy_id,
        ticker=ticker,
        side=side,
        quantity=qty,
        price=price,
        board=board,
        exec_key=f"{strategy_id}:{side}:{qty}:{price}:{ticker}:{board}",
    )
    save_order(order)


class TestStrategyPositionBook:

    def test_position_book_tracks_open_lots_and_avg_entry(self):
        _save(SID_1, "buy", 2, 100.0)
        _save(SID_1, "buy", 2, 110.0)
        _save(SID_1, "sell", 1, 120.0)

        entries = get_strategy_position_book(SID_1)

        assert len(entries) == 1
        entry = entries[0]
        assert entry["side"] == "buy"
        assert entry["quantity"] == 3
        assert entry["avg_entry_price"] == 106.66666666666667
        assert [lot["quantity"] for lot in entry["open_lots"]] == [1, 2]

    def test_strategy_history_closes_only_own_position(self):
        _save(SID_1, "buy", 2, 100.0)
        _save(SID_2, "buy", 4, 200.0)
        _save(SID_1, "sell", 2, 120.0)

        pos_one = get_strategy_position(SID_1, ticker="SBER", board="TQBR")
        pos_two = get_strategy_position(SID_2, ticker="SBER", board="TQBR")

        assert pos_one["quantity"] == 0
        assert pos_two["quantity"] == 4
        assert pos_two["avg_entry_price"] == 200.0


class TestStrategyFlattenPlanner:

    def test_plan_uses_only_strategy_owned_qty(self):
        _save(SID_1, "buy", 5, 100.0)
        _save(SID_2, "buy", 7, 100.0)

        strategy_map = {
            SID_1: {"account_id": "ACC", "connector_id": "finam"},
            SID_2: {"account_id": "ACC", "connector_id": "finam"},
        }

        with patch("core.strategy_flatten.get_strategy", side_effect=strategy_map.get):
            plan = build_strategy_flatten_plan(SID_1, ticker="SBER", board="TQBR")

        assert len(plan["items"]) == 1
        item = plan["items"][0]
        assert item["open_qty"] == 5
        assert item["close_qty"] == 5
        assert item["close_side"] == "sell"
        assert item["account_id"] == "ACC"


class TestStrategyFlattenExecutor:

    def test_executor_uses_strategy_id_as_agent_name(self):
        _save(SID_1, "buy", 4, 100.0)
        connector = _bind_result_api(MagicMock())
        connector.is_connected.return_value = True
        connector.get_positions.return_value = [{"ticker": "SBER", "board": "TQBR", "quantity": 4}]
        connector.close_position.return_value = "tid-1"

        with patch("core.strategy_flatten.get_strategy", return_value={"account_id": "ACC", "connector_id": "finam"}):
            result = StrategyFlattenExecutor(connector_resolver=lambda _: connector).execute(
                SID_1,
                ticker="SBER",
                board="TQBR",
                wait_for_confirmation=False,
            )

        assert result["status"] == "submitted"
        connector.close_position.assert_called_once_with(
            account_id="ACC",
            ticker="SBER",
            quantity=4,
            agent_name=SID_1,
        )

    def test_executor_respects_partial_strategy_flatten_qty(self):
        _save(SID_1, "buy", 5, 100.0)
        connector = _bind_result_api(MagicMock())
        connector.is_connected.return_value = True
        connector.get_positions.return_value = [{"ticker": "SBER", "board": "TQBR", "quantity": 5}]
        connector.close_position.return_value = "tid-2"

        with patch("core.strategy_flatten.get_strategy", return_value={"account_id": "ACC", "connector_id": "finam"}):
            result = StrategyFlattenExecutor(connector_resolver=lambda _: connector).execute(
                SID_1,
                ticker="SBER",
                board="TQBR",
                quantity=2,
                wait_for_confirmation=False,
            )

        assert result["status"] == "submitted"
        connector.close_position.assert_called_once_with(
            account_id="ACC",
            ticker="SBER",
            quantity=2,
            agent_name=SID_1,
        )

    def test_executor_rejects_broker_qty_below_strategy_qty(self):
        _save(SID_1, "buy", 5, 100.0)
        connector = _bind_result_api(MagicMock())
        connector.is_connected.return_value = True
        connector.get_positions.return_value = [{"ticker": "SBER", "board": "TQBR", "quantity": 3}]

        with patch("core.strategy_flatten.get_strategy", return_value={"account_id": "ACC", "connector_id": "finam"}):
            result = StrategyFlattenExecutor(connector_resolver=lambda _: connector).execute(
                SID_1,
                ticker="SBER",
                board="TQBR",
                wait_for_confirmation=False,
            )

        assert result["status"] == "manual_intervention_required"
        connector.close_position.assert_not_called()

    def test_executor_waits_for_strategy_target_confirmation(self):
        _save(SID_1, "buy", 5, 100.0)
        connector = _bind_result_api(MagicMock())
        connector.is_connected.return_value = True
        connector.get_positions.return_value = [{"ticker": "SBER", "board": "TQBR", "quantity": 5}]
        connector.close_position.return_value = "tid-3"
        connector.get_order_status.return_value = {"status": "matched"}

        quantities = iter([5, 4, 3])

        def _position_reader(*args, **kwargs):
            try:
                quantity = next(quantities)
            except StopIteration:
                quantity = 3
            return {
                "strategy_id": SID_1,
                "ticker": "SBER",
                "board": "TQBR",
                "side": "buy",
                "quantity": quantity,
                "avg_entry_price": 100.0,
                "entry_commission_total": 0.0,
                "open_lots": [],
            }

        with patch("core.strategy_flatten.get_strategy", return_value={"account_id": "ACC", "connector_id": "finam"}):
            result = StrategyFlattenExecutor(
                connector_resolver=lambda _: connector,
                position_reader=_position_reader,
                sleep_func=lambda _: None,
            ).execute(
                SID_1,
                ticker="SBER",
                board="TQBR",
                quantity=2,
                wait_for_confirmation=True,
                timeout_sec=0.1,
                poll_interval=0.0,
            )

        assert result["status"] == "success"
        assert result["items"][0]["remaining_qty"] == 3

    def test_executor_continues_flatten_after_partial_fill(self):
        _save(SID_1, "buy", 5, 100.0)
        connector = _bind_result_api(MagicMock())
        connector.is_connected.return_value = True
        connector.get_positions.return_value = [{"ticker": "SBER", "board": "TQBR", "quantity": 5}]
        connector.get_order_status.return_value = {"status": "matched"}

        state = {"attempt": 0}

        def _close_position(**kwargs):
            state["attempt"] += 1
            return f"tid-{state['attempt']}"

        def _position_reader(*args, **kwargs):
            quantity = 4 if state["attempt"] == 1 else 3
            return {
                "strategy_id": SID_1,
                "ticker": "SBER",
                "board": "TQBR",
                "side": "buy",
                "quantity": quantity,
                "avg_entry_price": 100.0,
                "entry_commission_total": 0.0,
                "open_lots": [],
            }

        connector.close_position.side_effect = _close_position

        with patch("core.strategy_flatten.get_strategy", return_value={"account_id": "ACC", "connector_id": "finam"}):
            result = StrategyFlattenExecutor(
                connector_resolver=lambda _: connector,
                position_reader=_position_reader,
                sleep_func=lambda _: None,
            ).execute(
                SID_1,
                ticker="SBER",
                board="TQBR",
                quantity=2,
                wait_for_confirmation=True,
                timeout_sec=0.01,
                poll_interval=0.0,
                max_child_orders=3,
            )

        assert result["status"] == "success"
        assert result["items"][0]["child_orders"] == 2


class TestPositionManagerStrategyClose:

    def test_position_manager_delegates_to_strategy_flatten_executor(self):
        pm = PositionManager()
        executor = MagicMock()
        executor.execute.return_value = {"status": "submitted"}

        with patch("core.strategy_flatten.StrategyFlattenExecutor", return_value=executor):
            result = pm.close_strategy_position(SID_1, ticker="SBER", quantity=2)

        assert result["status"] == "submitted"
        executor.execute.assert_called_once_with(
            strategy_id=SID_1,
            ticker="SBER",
            quantity=2,
            wait_for_confirmation=False,
        )
