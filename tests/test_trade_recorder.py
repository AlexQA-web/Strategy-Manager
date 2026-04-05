# tests/test_trade_recorder.py

"""Unit-тесты для core/trade_recorder.py"""

from unittest.mock import MagicMock, patch
import pytest

from core.trade_recorder import TradeRecorder


class MockOrderHistory:
    """Mock для order_history функций."""

    def __init__(self):
        self.orders = []

    def make_order(self, **kwargs):
        return kwargs

    def save_order(self, order):
        self.orders.append(order)

    def get_total_pnl(self, strategy_id):
        return 0.0


class MockStorage:
    """Mock для storage функций."""

    def __init__(self):
        self.trades = []

    def append_trade(self, trade):
        self.trades.append(trade)


class TestTradeRecorderInit:
    """Тесты инициализации TradeRecorder."""

    def test_default_init(self):
        """Проверяет базовую инициализацию."""
        recorder = TradeRecorder(
            strategy_id="test",
            ticker="SBER",
            board="TQBR",
            agent_name="test_agent",
        )
        assert recorder._strategy_id == "test"
        assert recorder._ticker == "SBER"


class TestRecordTrade:
    """Тесты метода record_trade."""

    def test_record_trade_success(self):
        """Успешная запись сделки."""
        mock_history = MockOrderHistory()
        mock_storage = MockStorage()

        with patch("core.fill_ledger.make_order", mock_history.make_order), \
             patch("core.fill_ledger.save_order", mock_history.save_order), \
             patch("core.fill_ledger.append_trade", mock_storage.append_trade), \
             patch("core.trade_recorder.get_total_pnl", mock_history.get_total_pnl), \
             patch("core.trade_recorder.record_equity"):

            recorder = TradeRecorder(
                strategy_id="test",
                ticker="SBER",
                board="TQBR",
                agent_name="test_agent",
                calculate_commission=lambda t, q, p: 10.0,
            )

            recorder.record_trade("buy", 10, 150.0, "test", order_ref="exec_123")

            assert len(mock_history.orders) == 1
            assert len(mock_storage.trades) == 1
            assert mock_storage.trades[0]["side"] == "buy"
            assert mock_storage.trades[0]["qty"] == 10
            assert mock_storage.trades[0]["price"] == 150.0

    def test_record_trade_no_exec_id(self):
        """Сделка без execution_id не записывается."""
        mock_history = MockOrderHistory()
        mock_storage = MockStorage()

        with patch("core.fill_ledger.make_order", mock_history.make_order), \
             patch("core.fill_ledger.save_order", mock_history.save_order), \
             patch("core.fill_ledger.append_trade", mock_storage.append_trade), \
             patch("core.trade_recorder.get_total_pnl", mock_history.get_total_pnl), \
             patch("core.trade_recorder.record_equity"):

            recorder = TradeRecorder(
                strategy_id="test",
                ticker="SBER",
                board="TQBR",
                agent_name="test_agent",
            )

            recorder.record_trade("buy", 10, 150.0, "test")

            assert len(mock_history.orders) == 0
            assert len(mock_storage.trades) == 0

    def test_record_trade_with_commission(self):
        """Запись сделки с комиссией."""
        mock_history = MockOrderHistory()
        mock_storage = MockStorage()

        with patch("core.fill_ledger.make_order", mock_history.make_order), \
             patch("core.fill_ledger.save_order", mock_history.save_order), \
             patch("core.fill_ledger.append_trade", mock_storage.append_trade), \
             patch("core.trade_recorder.get_total_pnl", mock_history.get_total_pnl), \
             patch("core.trade_recorder.record_equity"):

            recorder = TradeRecorder(
                strategy_id="test",
                ticker="SBER",
                board="TQBR",
                agent_name="test_agent",
                calculate_commission=lambda t, q, p: 50.0,
            )

            recorder.record_trade("buy", 10, 150.0, "test", order_ref="exec_123")

            order = mock_history.orders[0]
            assert order["commission_total"] == 50.0
            assert order["commission"] == 5.0  # 50 / 10


class TestCalculateCommission:
    """Тесты расчёта комиссии."""

    def test_calculate_commission_with_func(self):
        """Расчёт комиссии с переданной функцией."""
        recorder = TradeRecorder(
            strategy_id="test",
            ticker="SBER",
            board="TQBR",
            agent_name="test_agent",
            calculate_commission=lambda t, q, p: q * p * 0.001,
        )

        result = recorder.calculate_commission("SBER", 100, 150.0)
        assert result == 15.0  # 100 * 150 * 0.001

    def test_calculate_commission_without_func(self):
        """Расчёт комиссии без функции — возвращает 0."""
        recorder = TradeRecorder(
            strategy_id="test",
            ticker="SBER",
            board="TQBR",
            agent_name="test_agent",
        )

        result = recorder.calculate_commission("SBER", 100, 150.0)
        assert result == 0.0


class TestGetPointCost:
    """Тесты получения point_cost."""

    def test_get_point_cost_with_func(self):
        """Получение point_cost с функцией."""
        recorder = TradeRecorder(
            strategy_id="test",
            ticker="SBER",
            board="TQBR",
            agent_name="test_agent",
            get_point_cost=lambda: 2.5,
        )

        assert recorder.get_point_cost() == 2.5

    def test_get_point_cost_default(self):
        """Получение point_cost по умолчанию."""
        recorder = TradeRecorder(
            strategy_id="test",
            ticker="SBER",
            board="TQBR",
            agent_name="test_agent",
        )

        assert recorder.get_point_cost() == 1.0


class TestRecordTradeTransaction:
    """Тесты транзакционного поведения record_trade (TASK-005)."""

    def test_save_order_failure_prevents_append_trade(self):
        """Если save_order упал — append_trade НЕ вызывается."""
        mock_history = MockOrderHistory()
        mock_storage = MockStorage()
        save_mock = MagicMock(side_effect=RuntimeError("disk error"))

        with patch("core.fill_ledger.make_order", mock_history.make_order), \
             patch("core.fill_ledger.save_order", save_mock), \
             patch("core.fill_ledger.append_trade", mock_storage.append_trade), \
             patch("core.trade_recorder.get_total_pnl", mock_history.get_total_pnl), \
             patch("core.trade_recorder.record_equity"):

            recorder = TradeRecorder(
                strategy_id="test",
                ticker="SBER",
                board="TQBR",
                agent_name="test_agent",
            )

            recorder.record_trade("buy", 10, 150.0, "test", order_ref="exec_123")

            save_mock.assert_called_once()
            assert len(mock_storage.trades) == 0  # append_trade не вызван

    def test_both_stores_written_on_success(self):
        """При успехе оба хранилища получают данные."""
        mock_history = MockOrderHistory()
        mock_storage = MockStorage()

        with patch("core.fill_ledger.make_order", mock_history.make_order), \
             patch("core.fill_ledger.save_order", mock_history.save_order), \
             patch("core.fill_ledger.append_trade", mock_storage.append_trade), \
             patch("core.trade_recorder.get_total_pnl", mock_history.get_total_pnl), \
             patch("core.trade_recorder.record_equity"):

            recorder = TradeRecorder(
                strategy_id="test",
                ticker="SBER",
                board="TQBR",
                agent_name="test_agent",
            )

            recorder.record_trade("sell", 5, 200.0, "close", order_ref="exec_456")

            assert len(mock_history.orders) == 1
            assert len(mock_storage.trades) == 1
            assert mock_storage.trades[0]["execution_id"] == "exec_456"

    def test_equity_flush_after_both_writes(self):
        """Equity flush вызывается только после обоих записей."""
        mock_history = MockOrderHistory()
        mock_storage = MockStorage()
        equity_mock = MagicMock()

        with patch("core.fill_ledger.make_order", mock_history.make_order), \
             patch("core.fill_ledger.save_order", mock_history.save_order), \
             patch("core.fill_ledger.append_trade", mock_storage.append_trade), \
             patch("core.trade_recorder.get_total_pnl", mock_history.get_total_pnl), \
             patch("core.trade_recorder.record_equity", equity_mock):

            recorder = TradeRecorder(
                strategy_id="test",
                ticker="SBER",
                board="TQBR",
                agent_name="test_agent",
            )

            recorder.record_trade("buy", 3, 100.0, "test", order_ref="exec_789")

            equity_mock.assert_called_once()
            assert len(mock_history.orders) == 1
            assert len(mock_storage.trades) == 1


class TestFlushEquityMultiplier:
    """Тесты корректного PnL multiplier в _flush_equity (TASK-034)."""

    def test_flush_equity_uses_lot_size_for_stocks(self):
        """Для акций unrealized PnL использует lot_size, а не point_cost."""
        equity_mock = MagicMock()

        with patch("core.trade_recorder.get_total_pnl", return_value=0.0), \
             patch("core.trade_recorder.record_equity", equity_mock):

            recorder = TradeRecorder(
                strategy_id="test",
                ticker="SBER",
                board="TQBR",
                agent_name="test_agent",
                get_point_cost=lambda: 1.0,
                get_lot_size=lambda: 10,
                is_futures=lambda: False,
                get_last_price=lambda: 110.0,
                get_position_qty=lambda: 5,
                get_entry_price=lambda: 100.0,
            )

            recorder._flush_equity()

            equity_mock.assert_called_once()
            # gross = (110 - 100) * 5 * 10 = 500.0 (lot_size=10, не point_cost=1.0)
            equity_value = equity_mock.call_args[0][1]
            assert equity_value == 500.0

    def test_flush_equity_uses_point_cost_for_futures(self):
        """Для фьючерсов unrealized PnL использует point_cost."""
        equity_mock = MagicMock()

        with patch("core.trade_recorder.get_total_pnl", return_value=0.0), \
             patch("core.trade_recorder.record_equity", equity_mock):

            recorder = TradeRecorder(
                strategy_id="test",
                ticker="Si",
                board="SPBFUT",
                agent_name="test_agent",
                get_point_cost=lambda: 1.0,
                get_lot_size=lambda: 1,
                is_futures=lambda: True,
                get_last_price=lambda: 85000.0,
                get_position_qty=lambda: 2,
                get_entry_price=lambda: 84000.0,
            )

            recorder._flush_equity()

            equity_mock.assert_called_once()
            # gross = (85000 - 84000) * 2 * 1.0 = 2000.0 (point_cost=1.0)
            equity_value = equity_mock.call_args[0][1]
            assert equity_value == 2000.0
