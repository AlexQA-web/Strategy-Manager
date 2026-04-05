"""Тесты для core/backtest_engine.py."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from core.backtest_engine import BacktestEngine, Trade, BacktestResult
from core.txt_loader import Bar


def _bar(ticker="SBER", dt=None, date_int=260101, time_min=600,
         o=100.0, h=105.0, l=95.0, c=102.0, vol=1000, weekday=1):
    """Создаёт Bar для тестов."""
    return Bar(
        ticker=ticker,
        dt=dt or datetime(2026, 1, 1, 10, 0),
        date_int=date_int,
        time_min=time_min,
        weekday=weekday,
        open=o, high=h, low=l, close=c, vol=vol,
    )


def _make_bars(n=20, base_price=100.0):
    """Создаёт N баров с растущей ценой."""
    bars = []
    for i in range(n):
        price = base_price + i
        bars.append(_bar(
            dt=datetime(2026, 1, 1 + i // 10, 10, i % 60),
            date_int=260101 + i // 10,
            time_min=600 + i,
            o=price, h=price + 2, l=price - 1, c=price + 1,
        ))
    return bars


def _mock_module(action_sequence=None):
    """Создаёт mock стратегии.

    action_sequence: список action/None для каждого вызова on_bar.
    """
    module = MagicMock()
    module.get_params.return_value = {
        "qty": {"default": 1},
        "commission": {"default": 0.0},
        "order_mode": {"default": "market"},
        "slippage": {"default": 0.0},
    }
    module.get_lookback.return_value = 50

    if action_sequence:
        signals = []
        for a in action_sequence:
            if a is None:
                signals.append({"action": None})
            else:
                signals.append({"action": a, "qty": 1, "comment": f"test {a}"})
        module.on_bar.side_effect = signals
    else:
        module.on_bar.return_value = {"action": None}

    return module


class TestBacktestEngineRun:
    """Тесты BacktestEngine.run()."""

    @patch("core.backtest_engine.MOEXClient")
    @patch("core.backtest_engine.instrument_classifier")
    @patch("core.backtest_engine.commission_manager")
    def test_run_no_signals(self, mock_comm_mgr, mock_classifier, mock_moex):
        """Бэктест без сигналов — 0 сделок."""
        mock_classifier.is_futures.return_value = False
        bars = _make_bars(10)
        loader = MagicMock()
        loader.load.return_value = bars
        module = _mock_module()

        engine = BacktestEngine(loader=loader)
        result = engine.run(module, "fake.txt")

        assert result.trades_count == 0
        assert result.total_net_pnl == 0.0
        assert result.bars_count == 10

    @patch("core.backtest_engine.MOEXClient")
    @patch("core.backtest_engine.instrument_classifier")
    @patch("core.backtest_engine.commission_manager")
    def test_run_buy_and_close(self, mock_comm_mgr, mock_classifier, mock_moex):
        """Buy на баре 2, close на баре 5 — 1 сделка."""
        mock_classifier.is_futures.return_value = False
        mock_comm_mgr.calculate.return_value = 0.0
        bars = _make_bars(10)
        loader = MagicMock()
        loader.load.return_value = bars

        # None, None, buy, None, None, close, None...
        actions = [None] * 2 + ["buy"] + [None] * 2 + ["close"] + [None] * 3
        module = _mock_module(actions)

        engine = BacktestEngine(loader=loader)
        result = engine.run(module, "fake.txt")

        assert result.trades_count == 1
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.direction == 1
        assert trade.is_closed

    @patch("core.backtest_engine.MOEXClient")
    @patch("core.backtest_engine.instrument_classifier")
    @patch("core.backtest_engine.commission_manager")
    def test_run_sell_and_close(self, mock_comm_mgr, mock_classifier, mock_moex):
        """Sell на баре 1, close на баре 3 — шорт."""
        mock_classifier.is_futures.return_value = False
        mock_comm_mgr.calculate.return_value = 0.0
        bars = _make_bars(10)
        loader = MagicMock()
        loader.load.return_value = bars

        actions = [None, "sell", None, "close"] + [None] * 5
        module = _mock_module(actions)

        engine = BacktestEngine(loader=loader)
        result = engine.run(module, "fake.txt")

        assert result.trades_count == 1
        trade = result.trades[0]
        assert trade.direction == -1

    @patch("core.backtest_engine.MOEXClient")
    @patch("core.backtest_engine.instrument_classifier")
    @patch("core.backtest_engine.commission_manager")
    def test_force_close_at_end(self, mock_comm_mgr, mock_classifier, mock_moex):
        """Незакрытая позиция закрывается принудительно."""
        mock_classifier.is_futures.return_value = False
        mock_comm_mgr.calculate.return_value = 0.0
        bars = _make_bars(5)
        loader = MagicMock()
        loader.load.return_value = bars

        # Buy на первом баре, без close
        actions = ["buy"] + [None] * 3
        module = _mock_module(actions)

        engine = BacktestEngine(loader=loader)
        result = engine.run(module, "fake.txt")

        assert result.trades_count == 1
        trade = result.trades[0]
        assert trade.is_closed
        assert "Force close" in trade.exit_comment

    @patch("core.backtest_engine.MOEXClient")
    @patch("core.backtest_engine.instrument_classifier")
    @patch("core.backtest_engine.commission_manager")
    def test_insufficient_bars_raises(self, mock_comm_mgr, mock_classifier, mock_moex):
        """Менее 2 баров — ValueError."""
        loader = MagicMock()
        loader.load.return_value = [_bar()]
        module = _mock_module()

        engine = BacktestEngine(loader=loader)
        with pytest.raises(ValueError, match="Недостаточно баров"):
            engine.run(module, "fake.txt")

    @patch("core.backtest_engine.MOEXClient")
    @patch("core.backtest_engine.instrument_classifier")
    @patch("core.backtest_engine.commission_manager")
    def test_stop_flag_interrupts(self, mock_comm_mgr, mock_classifier, mock_moex):
        """stop_flag=True прерывает бэктест."""
        mock_classifier.is_futures.return_value = False
        bars = _make_bars(10)
        loader = MagicMock()
        loader.load.return_value = bars
        module = _mock_module()

        engine = BacktestEngine(loader=loader)
        with pytest.raises(InterruptedError):
            engine.run(module, "fake.txt", stop_flag=lambda: True)

    @patch("core.backtest_engine.MOEXClient")
    @patch("core.backtest_engine.instrument_classifier")
    @patch("core.backtest_engine.commission_manager")
    def test_on_bar_exception_skipped(self, mock_comm_mgr, mock_classifier, mock_moex):
        """on_bar exception не ломает бэктест."""
        mock_classifier.is_futures.return_value = False
        bars = _make_bars(5)
        loader = MagicMock()
        loader.load.return_value = bars
        module = _mock_module()
        module.on_bar.side_effect = [
            RuntimeError("test error"),
            {"action": None},
            {"action": None},
            {"action": None},
        ]

        engine = BacktestEngine(loader=loader)
        result = engine.run(module, "fake.txt")
        assert result.trades_count == 0


class TestBacktestMetrics:
    """Тесты расчёта метрик."""

    def test_trade_is_closed(self):
        t = Trade(direction=1, qty=1, entry_dt=datetime.now(), entry_price=100.0,
                  entry_comment="")
        assert t.is_closed is False
        t.exit_dt = datetime.now()
        assert t.is_closed is True
