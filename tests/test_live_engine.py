"""Тесты для core/live_engine.py — _process_bar(), manual_close_position(), no-auto-close guarantees."""

import time
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock

import pandas as pd
import pytest

from core.base_connector import MarketDataEnvelope, OrderOutcome, OrderResult


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
                position=0, position_qty=0, timeframe="5m"):
            connector = MagicMock()
            connector.is_connected.return_value = True

            def _place_order_result(**call_kwargs):
                value = connector.place_order(**call_kwargs)
                if value:
                    transaction_id = value if isinstance(value, str) else "legacy-submit"
                    return OrderResult(OrderOutcome.SUCCESS, transaction_id=transaction_id)
                return OrderResult(OrderOutcome.REJECTED, message="mock_place_order_none")

            connector.place_order_result.side_effect = _place_order_result

            def _close_position_result(**call_kwargs):
                value = connector.close_position(**call_kwargs)
                if value:
                    transaction_id = value if isinstance(value, str) else "legacy-close"
                    return OrderResult(OrderOutcome.SUCCESS, transaction_id=transaction_id)
                return OrderResult(OrderOutcome.REJECTED, message="mock_close_position_none")

            connector.close_position_result.side_effect = _close_position_result

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
                timeframe=timeframe,
            )

            # Наполняем бары
            if bars:
                engine._bars = list(bars)
            else:
                base_dt = pd.Timestamp("2026-01-01 10:00")
                engine._bars = []
                for index in range(15):
                    bar_dt = base_dt + pd.Timedelta(minutes=5 * index)
                    open_price = 100 + index
                    close_price = open_price + 1
                    engine._bars.append(
                        {
                            "open": open_price,
                            "high": close_price + 2,
                            "low": open_price - 2,
                            "close": close_price,
                            "vol": 1000 + index,
                            "dt": bar_dt,
                            "date_int": int(bar_dt.strftime("%y%m%d")),
                            "time_min": bar_dt.hour * 60 + bar_dt.minute,
                            "weekday": bar_dt.isoweekday(),
                        }
                    )

            engine._last_price = 106.0
            engine._last_price_envelope = MarketDataEnvelope.build(
                source_ts=time.time(),
                receive_ts=time.time(),
                source_id="test",
                stale_after_ms=5000,
            )
            engine._order_executor._monitor_pool.submit = MagicMock()

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

    def test_process_bar_attaches_signal_timestamp_and_market_data(self, live_engine_factory):
        engine, conn, loaded, module = live_engine_factory(
            module_signal={"action": "buy", "qty": 1},
        )
        engine._order_executor = MagicMock()
        engine._sync_status = "synced"

        engine._process_bar()

        signal = engine._order_executor.execute_signal.call_args[0][0]
        assert signal["signal_ts"] > 0
        assert signal["market_data_envelope"]["status"] == "fresh"

    def test_process_bar_rejects_duplicate_bar_timestamps(self, live_engine_factory):
        bars = [
            {"open": 100, "high": 101, "low": 99, "close": 100, "vol": 1,
             "dt": pd.Timestamp("2026-01-01 10:00"), "date_int": 260101, "time_min": 600, "weekday": 4},
            {"open": 101, "high": 102, "low": 100, "close": 101, "vol": 1,
             "dt": pd.Timestamp("2026-01-01 10:05"), "date_int": 260101, "time_min": 605, "weekday": 4},
            {"open": 102, "high": 103, "low": 101, "close": 102, "vol": 1,
             "dt": pd.Timestamp("2026-01-01 10:05"), "date_int": 260101, "time_min": 605, "weekday": 4},
        ]
        engine, conn, loaded, module = live_engine_factory(bars=bars)
        engine._order_executor = MagicMock()

        engine._process_bar()

        engine._order_executor.execute_signal.assert_not_called()
        assert engine.runtime_state == "manual_intervention_required"

    def test_update_bar_state_rejects_older_timestamp_under_same_lock(self, live_engine_factory):
        engine, *_ = live_engine_factory()
        newer_dt = pd.Timestamp("2026-01-01 10:10")
        older_dt = pd.Timestamp("2026-01-01 10:05")

        updated, was_initial = engine._update_bar_state([{"dt": newer_dt}], newer_dt)
        assert updated is True
        assert was_initial is True

        updated, was_initial = engine._update_bar_state([{"dt": older_dt}], older_dt)

        assert updated is False
        assert was_initial is False
        assert engine._get_last_bar_dt() == newer_dt

    def test_validate_bars_rejects_broken_ohlc(self, live_engine_factory):
        engine, *_ = live_engine_factory()
        is_valid, error = engine._validate_bars([
            {
                "open": 100,
                "high": 99,
                "low": 98,
                "close": 101,
                "dt": pd.Timestamp("2026-01-01 10:00"),
            }
        ])

        assert is_valid is False
        assert error.startswith("broken_high")


class TestManualClosePosition:
    """Тесты manual_close_position() — ручное закрытие, вызывается только оператором."""

    def test_manual_close_long(self, live_engine_factory):
        """Закрытие длинной позиции."""
        engine, conn, loaded, module = live_engine_factory(
            position=1, position_qty=5,
        )
        conn.place_order.return_value = "tid-123"

        result = engine.manual_close_position()

        assert result == "success"
        conn.place_order.assert_called_once()
        call_kwargs = conn.place_order.call_args.kwargs
        assert call_kwargs["side"] == "sell"
        assert call_kwargs["quantity"] == 5
        assert call_kwargs["order_type"] == "market"

    def test_manual_close_short(self, live_engine_factory):
        """Закрытие короткой позиции."""
        engine, conn, loaded, module = live_engine_factory(
            position=-1, position_qty=-3,
        )
        conn.place_order.return_value = "tid-456"

        result = engine.manual_close_position()

        assert result == "success"
        call_kwargs = conn.place_order.call_args.kwargs
        assert call_kwargs["side"] == "buy"
        assert call_kwargs["quantity"] == 3

    def test_manual_close_no_position(self, live_engine_factory):
        """Нет позиции — возвращает no_position."""
        engine, conn, loaded, module = live_engine_factory(position=0)

        result = engine.manual_close_position()

        assert result == "no_position"
        conn.place_order.assert_not_called()

    def test_manual_close_handles_exception(self, live_engine_factory):
        """Ошибка при закрытии возвращает close_failed."""
        engine, conn, loaded, module = live_engine_factory(
            position=1, position_qty=1,
        )
        conn.place_order.side_effect = RuntimeError("broker error")

        result = engine.manual_close_position()

        assert result == "close_failed"

    def test_manual_close_skipped_when_order_in_flight(self, live_engine_factory):
        """Ручное закрытие пропускается если ордер уже в работе."""
        engine, conn, loaded, module = live_engine_factory(
            position=1, position_qty=5,
        )
        engine._position_tracker.set_order_in_flight(True)

        result = engine.manual_close_position()

        assert result == "order_in_flight"
        conn.place_order.assert_not_called()

    def test_manual_close_broker_rejects(self, live_engine_factory):
        """Брокер отклоняет ордер — возвращает close_failed."""
        engine, conn, loaded, module = live_engine_factory(
            position=1, position_qty=2,
        )
        conn.place_order.return_value = None

        result = engine.manual_close_position()

        assert result == "close_failed"


class TestNoAutoCloseGuarantees:
    """Regression-тесты: запрет auto-close при stop, timeout и circuit breaker.

    TASK-010: гарантия сохранения открытой позиции во всех трёх сценариях.
    """

    def test_stop_preserves_open_position(self, live_engine_factory):
        """stop() НЕ закрывает позицию — позиция остаётся открытой."""
        engine, conn, loaded, module = live_engine_factory(
            position=1, position_qty=5,
        )
        engine._running = True

        engine.stop()

        # place_order не вызывался — позиция не закрыта
        conn.place_order.assert_not_called()
        conn.close_position = MagicMock()
        conn.close_position.assert_not_called()
        # Позиция сохранена
        assert engine._position_tracker.get_position() == 1
        assert engine._position_tracker.get_position_qty() == 5

    def test_stop_no_close_position_on_stop_flag(self, live_engine_factory):
        """stop() не читает close_position_on_stop — флаг полностью удалён."""
        engine, conn, loaded, module = live_engine_factory(
            position=1, position_qty=3,
        )
        engine._running = True

        # stop() больше не обращается к storage вообще
        engine.stop()

        conn.place_order.assert_not_called()
        assert engine._position_tracker.get_position_qty() == 3

    def test_circuit_breaker_preserves_position(self, live_engine_factory):
        """Circuit breaker НЕ закрывает позицию, только блокирует новые ордера."""
        engine, conn, loaded, module = live_engine_factory(
            position=1, position_qty=5,
        )

        with patch("core.live_engine.notifier", create=True):
            engine._on_circuit_break()

        # Позиция не закрыта
        conn.place_order.assert_not_called()
        assert engine._position_tracker.get_position() == 1
        assert engine._position_tracker.get_position_qty() == 5
        # Статус изменён на stale
        assert engine._sync_status == "stale"
        assert engine.runtime_state == "manual_intervention_required"

    def test_timeout_preserves_position(self, live_engine_factory):
        """Серия timeout НЕ закрывает позицию, а переводит в degraded state."""
        engine, conn, loaded, module = live_engine_factory(
            position=1, position_qty=5,
        )
        engine._consecutive_timeouts = engine._MAX_CONSECUTIVE_TIMEOUTS - 1

        from core.live_engine import FuturesTimeoutError

        future = MagicMock()
        future.result.side_effect = FuturesTimeoutError()
        engine._history_pool.submit = MagicMock(return_value=future)

        with patch("core.live_engine.notifier", create=True):
            engine._load_and_update()

        conn.place_order.assert_not_called()
        conn.close_position.assert_not_called()
        assert engine._position_tracker.get_position() == 1
        assert engine._position_tracker.get_position_qty() == 5
        assert engine._sync_status == "stale"
        assert engine.runtime_state == "degraded"

    def test_stop_does_not_accept_close_position_parameter(self, live_engine_factory):
        """stop() не принимает параметр close_position."""
        engine, conn, loaded, module = live_engine_factory(
            position=1, position_qty=2,
        )
        engine._running = True

        import inspect
        sig = inspect.signature(engine.stop)
        assert "close_position" not in sig.parameters


class TestStartContract:
    """Regression-тесты контракта запуска LiveEngine."""

    def test_start_returns_false_when_futures_point_cost_missing(self, live_engine_factory):
        """Для фьючерса без point_cost start() возвращает False и не стартует поток."""
        engine, conn, loaded, module = live_engine_factory()
        engine._load_point_cost = MagicMock(return_value=False)

        with patch("core.live_engine.instrument_classifier.is_futures", return_value=True):
            started = engine.start()

        assert started is False
        assert engine._running is False
        conn.subscribe_reconnect.assert_not_called()

    def test_stop_unsubscribes_reconnect_listener(self, live_engine_factory):
        """stop() снимает reconnect-listener, чтобы restart не накапливал callbacks."""
        engine, conn, loaded, module = live_engine_factory()
        engine._running = True
        engine._subscribed_reconnect = True

        engine.stop()

        conn.unsubscribe_reconnect.assert_called_once_with(engine._on_connector_reconnect)

    def test_startup_preflight_sets_synced_runtime_state(self, live_engine_factory):
        """Preflight snapshot/detect/reconcile до старта переводит engine в synced state."""
        engine, conn, loaded, module = live_engine_factory(position=1, position_qty=5)
        conn.get_positions.return_value = [{
            "ticker": "SBER",
            "board": "TQBR",
            "quantity": 5,
            "avg_price": 100.0,
            "current_price": 101.0,
        }]
        conn.get_accounts.return_value = []
        conn.get_free_money.return_value = None
        engine._reconciler._get_order_pairs = lambda sid: [
            {
                "open": {"ticker": "SBER", "quantity": 5, "side": "buy"},
                "close": None,
            }
        ]

        started = engine.startup_preflight()

        assert started is True
        assert engine.sync_status == "synced"
        assert engine.runtime_state == "synced"

    def test_startup_preflight_failed_sets_failed_start_state(self, live_engine_factory):
        """Ошибка preflight переводит engine в failed_start."""
        engine, conn, loaded, module = live_engine_factory()

        with patch("core.live_engine.fetch_startup_snapshot", side_effect=RuntimeError("snapshot failed")):
            started = engine.startup_preflight()

        assert started is False
        assert engine.runtime_state == "failed_start"

    def test_startup_preflight_unresolved_pending_orders_require_manual_intervention(self, live_engine_factory):
        """Неразрешимые pending orders на старте блокируют запуск в manual_intervention_required."""
        engine, conn, loaded, module = live_engine_factory()

        with patch(
            "core.live_engine.fetch_startup_snapshot",
            return_value={
                "pending_recovery": {
                    "recovered": [],
                    "unresolved": [{"tid": "123", "reason": "missing_on_broker"}],
                }
            },
        ):
            started = engine.startup_preflight()

        assert started is False
        assert engine.sync_status == "stale"
        assert engine.runtime_state == "manual_intervention_required"

    def test_startup_preflight_allows_stale_startup(self, live_engine_factory):
        """Если detect_position не удался, startup завершается в degraded/stale state."""
        engine, conn, loaded, module = live_engine_factory()
        conn.get_positions.side_effect = RuntimeError("broker offline")

        with patch("core.live_engine.fetch_startup_snapshot", return_value={}):
            started = engine.startup_preflight()

        assert started is True
        assert engine.sync_status == "stale"
        assert engine.runtime_state == "degraded"


class TestWarmupHistory:

    def test_initial_warmup_days_scale_with_timeframe_and_lookback(self, live_engine_factory):
        engine, conn, loaded, module = live_engine_factory(timeframe="1h")
        module.get_lookback.return_value = 200

        initial_days = engine._calculate_initial_history_days(200)

        assert initial_days > 20
        assert initial_days > engine._calculate_incremental_history_days()

    def test_load_and_update_refetches_until_required_history_is_loaded(self, live_engine_factory):
        engine, conn, loaded, module = live_engine_factory(timeframe="1h")
        module.get_lookback.return_value = 200

        call_days = []

        def _frame(count: int) -> pd.DataFrame:
            index = pd.date_range("2026-01-01 10:00", periods=count, freq="h")
            return pd.DataFrame(
                {
                    "Open": [100.0 + i for i in range(count)],
                    "High": [101.0 + i for i in range(count)],
                    "Low": [99.0 + i for i in range(count)],
                    "Close": [100.5 + i for i in range(count)],
                    "Volume": [1000 + i for i in range(count)],
                },
                index=index,
            )

        def _get_history(**kwargs):
            call_days.append(kwargs["days"])
            return _frame(40 if len(call_days) == 1 else 220)

        conn.get_history.side_effect = _get_history

        engine._load_and_update()

        assert len(call_days) == 2
        assert call_days[1] > call_days[0]


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

    def test_detect_requires_manual_intervention_on_strategy_collision(self, live_engine_factory):
        engine, conn, loaded, module = live_engine_factory(position=1, position_qty=5)

        with patch("core.autostart.has_strategy_collision", return_value=True):
            engine._detect_position()

        assert engine.runtime_state == "manual_intervention_required"
        assert engine.sync_status == "stale"

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

    def test_detect_releases_submission_blocks_after_successful_reconcile(self, live_engine_factory):
        engine, conn, loaded, module = live_engine_factory(position=1, position_qty=5)
        conn.get_positions.return_value = []
        engine._order_executor._block_submission("dup-key", reason="ambiguous_submit")

        with patch("core.order_executor.pending_order_registry.get_pending", return_value=[]):
            engine._detect_position()

        assert engine._order_executor._is_submission_blocked("dup-key") is False


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
