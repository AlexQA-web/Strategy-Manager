"""Тесты для core/live_engine.py — _process_bar(), _emergency_close_position()."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock

import pandas as pd
import pytest


# Патчим модули ДО импорта LiveEngine, чтобы не тянуть реальные зависимости
@pytest.fixture
def live_engine_factory():
    """Фабрика для создания LiveEngine с замоканными зависимостями."""
    with patch("core.live_engine.connector_manager") as mock_cm, \
         patch("core.live_engine.commission_manager"), \
         patch("core.live_engine.instrument_classifier"), \
         patch("core.live_engine.get_total_pnl", return_value=0.0), \
         patch("core.live_engine.get_order_pairs", return_value=[]), \
         patch("core.live_engine.equity_flush_all"), \
         patch("core.live_engine.record_equity"), \
         patch("core.live_engine.get_max_drawdown", return_value=0.0):

        mock_cm.all.return_value = {}

        from core.live_engine import LiveEngine
        created_engines = []

        def _create(bars=None, module_signal=None, has_custom_execute=False,
                    position=0, position_qty=0):
            connector = MagicMock()
            connector.is_connected.return_value = True

            loaded = MagicMock()
            # SimpleNamespace вместо MagicMock: hasattr работает честно,
            # без авто-создания атрибутов (execute_signal и т.д.)
            module = SimpleNamespace()
            module.on_precalc = MagicMock(side_effect=lambda df, params: df)
            module.get_lookback = MagicMock(return_value=50)
            loaded.module = module

            if has_custom_execute:
                module.execute_signal = MagicMock()
                loaded.custom_execution_adapter = "test-adapter"
                loaded.custom_execution_actions = frozenset({"buy", "sell", "close", "snapshot"})
            else:
                loaded.custom_execution_adapter = None
                loaded.custom_execution_actions = frozenset()

            if module_signal is not None:
                loaded.call_on_bar.return_value = module_signal
            else:
                loaded.call_on_bar.return_value = {"action": None}

            engine = LiveEngine(
                strategy_id="test_strategy",
                loaded_strategy=loaded,
                params={"qty": 1},
                connector=connector,
                account_id="test_account",
                ticker="SBER",
                board="TQBR",
                timeframe="5m",
            )

            # Наполняем бары
            if bars:
                engine._bars = list(bars)
            else:
                engine._bars = [
                    {"open": 100, "high": 105, "low": 95, "close": 102, "vol": 1000,
                     "dt": "2026-01-01 10:00", "date_int": 260101, "time_min": 600, "weekday": 4},
                    {"open": 102, "high": 107, "low": 97, "close": 104, "vol": 1200,
                     "dt": "2026-01-01 10:05", "date_int": 260101, "time_min": 605, "weekday": 4},
                    {"open": 104, "high": 109, "low": 99, "close": 106, "vol": 1100,
                     "dt": "2026-01-01 10:10", "date_int": 260101, "time_min": 610, "weekday": 4},
                ] * 5  # 15 bars

            if position != 0:
                engine._position_tracker.update_position(
                    position, position_qty or 1, 100.0
                )

            created_engines.append(engine)

            return engine, connector, loaded, module

        yield _create

        for engine in created_engines:
            try:
                engine._order_executor.stop()
            except Exception:
                pass
            try:
                engine._cleanup_history_pool()
            except Exception:
                pass


class TestProcessBar:
    """Тесты LiveEngine._process_bar()."""

    def test_no_action_when_few_bars(self, live_engine_factory):
        """Менее 2 баров — ничего не делаем."""
        engine, conn, loaded, module = live_engine_factory(bars=[
            {"open": 100, "high": 105, "low": 95, "close": 102, "vol": 1000,
             "dt": "2026-01-01 10:00", "date_int": 260101, "time_min": 600, "weekday": 4},
        ])
        engine._process_bar()
        loaded.call_on_bar.assert_not_called()

    def test_on_bar_called_with_signal(self, live_engine_factory):
        """on_bar вызывается и сигнал обрабатывается."""
        engine, conn, loaded, module = live_engine_factory(
            module_signal={"action": None}
        )
        engine._process_bar()
        loaded.call_on_bar.assert_called_once()

    def test_buy_signal_delegates_to_order_executor(self, live_engine_factory):
        """Buy-сигнал делегируется order_executor."""
        engine, conn, loaded, module = live_engine_factory(
            module_signal={"action": "buy", "qty": 1, "comment": "test buy"},
        )
        engine._order_executor = MagicMock()
        engine._sync_status = "synced"

        engine._process_bar()

        engine._order_executor.execute_signal.assert_called_once()
        call_args = engine._order_executor.execute_signal.call_args[0][0]
        assert call_args["action"] == "buy"

    def test_buy_ignored_when_already_in_position(self, live_engine_factory):
        """Buy-сигнал игнорируется если позиция уже открыта."""
        engine, conn, loaded, module = live_engine_factory(
            module_signal={"action": "buy", "qty": 1},
            position=1, position_qty=1,
        )
        engine._order_executor = MagicMock()

        engine._process_bar()

        engine._order_executor.execute_signal.assert_not_called()

    def test_close_signal_processed(self, live_engine_factory):
        """Close-сигнал обрабатывается."""
        engine, conn, loaded, module = live_engine_factory(
            module_signal={"action": "close", "qty": 1},
            position=1, position_qty=1,
        )
        engine._order_executor = MagicMock()

        engine._process_bar()

        engine._order_executor.execute_signal.assert_called_once()

    def test_custom_execute_signal(self, live_engine_factory):
        """Для registered adapter используется custom execute_signal."""
        engine, conn, loaded, module = live_engine_factory(
            module_signal={"action": "buy", "qty": 1},
            has_custom_execute=True,
        )
        engine._sync_status = "synced"

        engine._process_bar()

        module.execute_signal.assert_called_once()

    def test_unregistered_execute_signal_falls_back_to_order_executor(self, live_engine_factory):
        """Само наличие execute_signal не даёт bypass без explicit adapter registration."""
        engine, conn, loaded, module = live_engine_factory(
            module_signal={"action": "buy", "qty": 1},
            has_custom_execute=False,
        )
        module.execute_signal = MagicMock()
        engine._order_executor = MagicMock()
        engine._sync_status = "synced"

        engine._process_bar()

        module.execute_signal.assert_not_called()
        engine._order_executor.execute_signal.assert_called_once()

    def test_registered_adapter_allows_adapter_specific_action(self, live_engine_factory):
        """Registered adapter может использовать свой action из registry metadata."""
        engine, conn, loaded, module = live_engine_factory(
            module_signal={"action": "snapshot"},
            has_custom_execute=True,
        )

        engine._process_bar()

        module.execute_signal.assert_called_once()

    def test_custom_execute_signal_blocked_by_circuit_breaker(self, live_engine_factory):
        """Custom execute_signal блокируется при открытом circuit breaker."""
        engine, conn, loaded, module = live_engine_factory(
            module_signal={"action": "buy", "qty": 1},
            has_custom_execute=True,
        )
        engine._sync_status = "synced"
        engine._risk_guard._circuit_open = True

        engine._process_bar()

        module.execute_signal.assert_not_called()

    def test_custom_execute_signal_blocked_by_risk_limits(self, live_engine_factory):
        """Custom execute_signal блокируется при нарушении лимитов риска."""
        engine, conn, loaded, module = live_engine_factory(
            module_signal={"action": "sell", "qty": 100},
            has_custom_execute=True,
        )
        engine._sync_status = "synced"
        engine._risk_guard._max_position_size = 10

        engine._process_bar()

        module.execute_signal.assert_not_called()

    def test_custom_execute_signal_close_allowed_when_circuit_open(self, live_engine_factory):
        """Custom execute_signal с close разрешён при открытом circuit breaker."""
        engine, conn, loaded, module = live_engine_factory(
            module_signal={"action": "close", "qty": 1},
            has_custom_execute=True,
            position=1, position_qty=1,
        )
        engine._risk_guard._circuit_open = True

        engine._process_bar()

        module.execute_signal.assert_called_once()

    def test_precalc_exception_handled(self, live_engine_factory):
        """Ошибка в precalc не ломает engine."""
        engine, conn, loaded, module = live_engine_factory()
        module.on_precalc.side_effect = RuntimeError("precalc error")

        # Не должно бросить исключение
        engine._process_bar()
        loaded.call_on_bar.assert_not_called()


class TestEmergencyClose:
    """Тесты _emergency_close_position()."""

    def test_emergency_close_long(self, live_engine_factory):
        """Закрытие длинной позиции."""
        engine, conn, loaded, module = live_engine_factory(
            position=1, position_qty=5,
        )
        conn.place_order.return_value = "tid-123"

        with patch("core.live_engine.notifier", create=True):
            engine._emergency_close_position()

        conn.place_order.assert_called_once()
        call_kwargs = conn.place_order.call_args.kwargs
        assert call_kwargs["side"] == "sell"
        assert call_kwargs["quantity"] == 5
        assert call_kwargs["order_type"] == "market"

    def test_emergency_close_short(self, live_engine_factory):
        """Закрытие короткой позиции."""
        engine, conn, loaded, module = live_engine_factory(
            position=-1, position_qty=-3,
        )
        conn.place_order.return_value = "tid-456"

        with patch("core.live_engine.notifier", create=True):
            engine._emergency_close_position()

        call_kwargs = conn.place_order.call_args.kwargs
        assert call_kwargs["side"] == "buy"
        assert call_kwargs["quantity"] == 3

    def test_emergency_close_no_position(self, live_engine_factory):
        """Нет позиции — ничего не делаем."""
        engine, conn, loaded, module = live_engine_factory(position=0)

        engine._emergency_close_position()

        conn.place_order.assert_not_called()

    def test_emergency_close_handles_exception(self, live_engine_factory):
        """Ошибка при закрытии не ломает engine."""
        engine, conn, loaded, module = live_engine_factory(
            position=1, position_qty=1,
        )
        conn.place_order.side_effect = RuntimeError("broker error")

        with patch("core.live_engine.notifier", create=True):
            # Не должно бросить исключение
            engine._emergency_close_position()

    def test_emergency_close_skipped_when_order_in_flight(self, live_engine_factory):
        """Аварийное закрытие пропускается если ордер уже в работе."""
        engine, conn, loaded, module = live_engine_factory(
            position=1, position_qty=5,
        )
        engine._position_tracker.set_order_in_flight(True)

        engine._emergency_close_position()

        conn.place_order.assert_not_called()


class TestDetectPosition:
    """Тесты LiveEngine._detect_position() — защита от destructive reset."""

    def test_detect_preserves_position_on_error(self, live_engine_factory):
        """При ошибке get_positions позиция НЕ сбрасывается."""
        engine, conn, loaded, module = live_engine_factory(
            position=1, position_qty=5,
        )
        engine._position_tracker.update_position(1, 5, 150.0)
        conn.get_positions.side_effect = RuntimeError("connection lost")

        # Не должно бросить исключение
        engine._detect_position()

        # Позиция сохранена
        assert engine._position_tracker.get_position() == 1
        assert engine._position_tracker.get_position_qty() == 5

    def test_detect_updates_position_on_success(self, live_engine_factory):
        """При успешном get_positions позиция обновляется."""
        engine, conn, loaded, module = live_engine_factory(
            position=1, position_qty=5,
        )
        conn.get_positions.return_value = [
            {"ticker": "SBER", "quantity": -3, "avg_price": 200.0, "current_price": 195.0}
        ]

        engine._detect_position()

        assert engine._position_tracker.get_position() == -1
        assert engine._position_tracker.get_position_qty() == -3

    def test_detect_zeroes_out_when_no_position_found(self, live_engine_factory):
        """Если тикер не найден в позициях — позиция обнуляется (это подтверждённые данные)."""
        engine, conn, loaded, module = live_engine_factory(
            position=1, position_qty=5,
        )
        conn.get_positions.return_value = [
            {"ticker": "GAZP", "quantity": 10, "avg_price": 300.0, "current_price": 310.0}
        ]

        engine._detect_position()

        assert engine._position_tracker.get_position() == 0
        assert engine._position_tracker.get_position_qty() == 0


class TestDegradedState:
    """Тесты degraded trading state при stale broker data (TASK-039)."""

    def test_sync_status_starts_unknown(self, live_engine_factory):
        """Начальный sync_status == 'unknown'."""
        engine, *_ = live_engine_factory()
        assert engine.sync_status == "unknown"

    def test_detect_success_sets_synced(self, live_engine_factory):
        """Успешный _detect_position → sync_status == 'synced'."""
        engine, conn, *_ = live_engine_factory()
        conn.get_positions.return_value = []
        engine._detect_position()
        assert engine.sync_status == "synced"

    def test_detect_error_sets_stale(self, live_engine_factory):
        """Ошибка _detect_position → sync_status == 'stale'."""
        engine, conn, *_ = live_engine_factory()
        conn.get_positions.side_effect = RuntimeError("connection lost")
        engine._detect_position()
        assert engine.sync_status == "stale"

    def test_buy_blocked_when_stale(self, live_engine_factory):
        """При stale сигнал buy отвергается."""
        engine, conn, loaded, module = live_engine_factory(
            module_signal={"action": "buy", "qty": 1},
        )
        engine._sync_status = "stale"
        engine._process_bar()
        loaded.call_on_bar.assert_called_once()
        conn.place_order.assert_not_called()

    def test_sell_blocked_when_stale(self, live_engine_factory):
        """При stale сигнал sell отвергается."""
        engine, conn, loaded, module = live_engine_factory(
            module_signal={"action": "sell", "qty": 1},
        )
        engine._sync_status = "stale"
        engine._process_bar()
        conn.place_order.assert_not_called()

    def test_buy_blocked_when_unknown(self, live_engine_factory):
        """При unknown (до первого sync) сигнал buy отвергается."""
        engine, conn, loaded, module = live_engine_factory(
            module_signal={"action": "buy", "qty": 1},
        )
        assert engine.sync_status == "unknown"
        engine._process_bar()
        conn.place_order.assert_not_called()

    def test_close_allowed_when_stale(self, live_engine_factory):
        """При stale сигнал close разрешён (risk-reduction)."""
        engine, conn, loaded, module = live_engine_factory(
            module_signal={"action": "close", "qty": 1},
            position=1, position_qty=1,
        )
        engine._sync_status = "stale"
        conn.close_position.return_value = "tid-1"
        conn.get_order_status.return_value = {"status": "matched", "balance": 0, "quantity": 1}
        engine._process_bar()
        # close должен был пройти
        assert conn.close_position.called or conn.place_order.called

    def test_buy_allowed_when_synced(self, live_engine_factory):
        """При synced сигнал buy проходит нормально."""
        engine, conn, loaded, module = live_engine_factory(
            module_signal={"action": "buy", "qty": 1},
        )
        engine._sync_status = "synced"
        conn.place_order.return_value = "tid-1"
        conn.get_order_status.return_value = {"status": "matched", "balance": 0, "quantity": 1}
        engine._process_bar()
        conn.place_order.assert_called_once()

    def test_resync_after_stale(self, live_engine_factory):
        """После stale успешный _detect_position возвращает synced."""
        engine, conn, *_ = live_engine_factory()
        conn.get_positions.side_effect = RuntimeError("fail")
        engine._detect_position()
        assert engine.sync_status == "stale"

        conn.get_positions.side_effect = None
        conn.get_positions.return_value = []
        engine._detect_position()
        assert engine.sync_status == "synced"

    def test_broker_unavailable_callback_sets_stale(self, live_engine_factory):
        """Callback _on_broker_unavailable переводит в stale."""
        engine, *_ = live_engine_factory()
        engine._sync_status = "synced"
        engine._on_broker_unavailable()
        assert engine.sync_status == "stale"


class TestManualCommission:
    """Regression-тесты manual commission formula (TASK-035)."""

    def test_stock_manual_commission_includes_lot_size(self, live_engine_factory):
        """Manual комиссия для акций учитывает lot_size.

        SBER: price=300, qty=2 лота, lot_size=10,
        commission_pct=0.05% → trade_value = 300 * 2 * 10 = 6000
        commission = 6000 * 0.0005 = 3.0
        """
        engine, *_ = live_engine_factory()
        engine._commission_pct = 0.05
        engine._lot_size = 10
        result = engine._calculate_commission_manual("SBER", 2, 300.0, "stock")
        assert result == pytest.approx(3.0)

    def test_stock_manual_commission_lot_size_1(self, live_engine_factory):
        """Manual комиссия c lot_size=1.

        price=100, qty=5, lot_size=1, pct=0.1%
        trade_value = 100 * 5 * 1 = 500
        commission = 500 * 0.001 = 0.5
        """
        engine, *_ = live_engine_factory()
        engine._commission_pct = 0.1
        engine._lot_size = 1
        result = engine._calculate_commission_manual("GAZP", 5, 100.0, "stock")
        assert result == pytest.approx(0.5)

    def test_futures_manual_commission_per_lot(self, live_engine_factory):
        """Manual комиссия для фьючерсов — руб/контракт.

        commission_rub=2.0, qty=3 → 6.0
        """
        engine, *_ = live_engine_factory()
        engine._commission_rub = 2.0
        result = engine._calculate_commission_manual("SiM6", 3, 90000.0, "futures")
        assert result == pytest.approx(6.0)

    def test_etf_manual_commission_includes_lot_size(self, live_engine_factory):
        """ETF — то же поведение, что и акции.

        price=50, qty=10, lot_size=100, pct=0.04%
        trade_value = 50 * 10 * 100 = 50000
        commission = 50000 * 0.0004 = 20.0
        """
        engine, *_ = live_engine_factory()
        engine._commission_pct = 0.04
        engine._lot_size = 100
        result = engine._calculate_commission_manual("TMOS", 10, 50.0, "etf")
        assert result == pytest.approx(20.0)
