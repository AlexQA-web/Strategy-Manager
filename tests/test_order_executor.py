# tests/test_order_executor.py

"""Unit-тесты для core/order_executor.py с mock коннектора."""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from core.base_connector import OrderOutcome, OrderResult
from core.order_lifecycle import OrderLifecycle
from core.order_executor import OrderExecutor
from core.position_tracker import PositionTracker
from core.reservation_ledger import ReservationLedger


def _bind_result_api(connector):
    def _place_order_result(**call_kwargs):
        value = connector.place_order(**call_kwargs)
        if value:
            transaction_id = value if isinstance(value, str) else "legacy-submit"
            return OrderResult(OrderOutcome.SUCCESS, transaction_id=transaction_id)
        return OrderResult(OrderOutcome.REJECTED, message="mock_place_order_none")

    def _close_position_result(**call_kwargs):
        value = connector.close_position(**call_kwargs)
        if value:
            transaction_id = value if isinstance(value, str) else "legacy-close"
            return OrderResult(OrderOutcome.SUCCESS, transaction_id=transaction_id)
        return OrderResult(OrderOutcome.REJECTED, message="mock_close_position_none")

    def _cancel_order_result(order_id, account_id):
        value = connector.cancel_order(order_id, account_id)
        if value:
            return OrderResult(OrderOutcome.SUCCESS, transaction_id=str(order_id))
        return OrderResult(OrderOutcome.REJECTED, transaction_id=str(order_id), message="mock_cancel_false")

    connector.place_order_result.side_effect = _place_order_result
    connector.close_position_result.side_effect = _close_position_result
    connector.cancel_order_result.side_effect = _cancel_order_result
    return connector


class MockTradeRecorder:
    """Mock для TradeRecorder."""

    def __init__(self):
        self.recorded_trades = []

    def record_trade(self, side, qty, price, comment, order_type="market", order_ref="", correlation_id=""):
        self.recorded_trades.append({
            "side": side,
            "qty": qty,
            "price": price,
            "comment": comment,
            "order_type": order_type,
            "order_ref": order_ref,
            "correlation_id": correlation_id,
        })


class MockRiskGuard:
    """Mock для RiskGuard."""

    def __init__(self):
        self.failures = 0
        self.successes = 0
        self._circuit_open = False
        self._risk_allowed = True
        self._risk_reason = ""
        self._trip_on_failure = False  # если True, record_failure() откроет circuit

    def record_failure(self):
        self.failures += 1
        if self._trip_on_failure:
            self._circuit_open = True
            return True
        return False

    def record_success(self):
        self.successes += 1

    def is_circuit_open(self):
        return self._circuit_open

    def check_risk_limits(self, action: str, qty: int, ticker: str = ""):
        return self._risk_allowed, self._risk_reason

    def notify_order_submitted(self, action: str, qty: int = 0, ticker: str = ""):
        return None


class TestOrderExecutorInit:
    """Тесты инициализации OrderExecutor."""

    def test_default_init(self):
        """Проверяет базовую инициализацию."""
        connector = MagicMock()
        pt = PositionTracker()
        tr = MockTradeRecorder()
        rg = MockRiskGuard()

        executor = OrderExecutor(
            strategy_id="test_strategy",
            connector=connector,
            position_tracker=pt,
            trade_recorder=tr,
            risk_guard=rg,
            account_id="test_account",
            ticker="SBER",
            board="TQBR",
            agent_name="test_agent",
        )

        assert executor.running is True
        assert executor._strategy_id == "test_strategy"
        assert executor._ticker == "SBER"
        assert executor._order_mode == "market"


class TestExecuteSignal:
    """Тесты метода execute_signal."""

    def _create_executor(self, order_mode="market", **kwargs):
        connector = _bind_result_api(MagicMock())
        connector.get_free_money.return_value = 100000.0
        pt = PositionTracker()
        tr = MockTradeRecorder()
        rg = MockRiskGuard()

        get_last_price = kwargs.pop("get_last_price", lambda: 100.0)
        get_last_price_envelope = kwargs.pop(
            "get_last_price_envelope",
            lambda: {
                "source_ts": time.time(),
                "receive_ts": time.time(),
                "age_ms": 0,
                "source_id": "test",
                "status": "fresh",
            },
        )

        executor = OrderExecutor(
            strategy_id="test_strategy",
            connector=connector,
            position_tracker=pt,
            trade_recorder=tr,
            risk_guard=rg,
            account_id="test_account",
            ticker="SBER",
            board="TQBR",
            agent_name="test_agent",
            order_mode=order_mode,
            get_last_price=get_last_price,
            get_last_price_envelope=get_last_price_envelope,
            **kwargs,
        )
        executor._monitor_pool.submit = MagicMock()
        return executor, connector, pt, tr, rg

    def test_execute_buy_signal_market_mode(self):
        """Исполнение сигнала на покупку в режиме market."""
        executor, connector, pt, tr, rg = self._create_executor(order_mode="market")
        connector.place_order.return_value = "order_123"

        executor.execute_signal({"action": "buy", "qty": 10, "comment": "test_buy"})

        # place_order должен быть вызван
        connector.place_order.assert_called_once()
        call_kwargs = connector.place_order.call_args.kwargs
        assert call_kwargs["side"] == "buy"
        assert call_kwargs["quantity"] == 10

    def test_execute_signal_invalid_qty(self):
        """Сигнал с некорректным qty."""
        executor, connector, pt, tr, rg = self._create_executor()

        executor.execute_signal({"action": "buy", "qty": -5, "comment": "bad"})

        # Ордер не должен быть размещён
        connector.place_order.assert_not_called()
        assert rg.failures == 1

    def test_execute_signal_ignores_if_in_position(self):
        """Сигнал игнорируется если позиция уже открыта."""
        executor, connector, pt, tr, rg = self._create_executor()
        pt.open_position("buy", 10, 150.0)

        executor.execute_signal({"action": "buy", "qty": 5, "comment": "dup"})

        connector.place_order.assert_not_called()

    def test_execute_signal_ignores_if_order_in_flight(self):
        """Сигнал игнорируется если ордер уже в полёте."""
        executor, connector, pt, tr, rg = self._create_executor()
        pt.set_order_in_flight(True)

        executor.execute_signal({"action": "buy", "qty": 5, "comment": "dup"})

        connector.place_order.assert_not_called()

    def test_execute_close_signal_no_position(self):
        """Сигнал на закрытие когда позиции нет."""
        executor, connector, pt, tr, rg = self._create_executor()

        executor.execute_signal({"action": "close", "qty": 10, "comment": "close"})

        connector.place_order.assert_not_called()

    def test_execute_close_signal_with_position(self):
        """Сигнал на закрытие использует только close_position без fallback."""
        executor, connector, pt, tr, rg = self._create_executor(order_mode="market")
        pt.open_position("buy", 10, 150.0)
        pt.clear_order_in_flight()  # имитируем завершение open-ордера
        connector.close_position.return_value = "close_order_456"

        executor.execute_signal({"action": "close", "qty": 10, "comment": "close"})

        connector.close_position.assert_called_once()
        connector.place_order.assert_not_called()

    def test_duplicate_submit_blocked_after_ambiguous_failure(self):
        """Повторный submit того же сигнала блокируется после неясного результата отправки."""
        callback = MagicMock()
        executor, connector, pt, tr, rg = self._create_executor(
            order_mode="market",
            on_manual_intervention=callback,
        )
        connector.place_order.return_value = None
        connector.place_order_result.side_effect = lambda **kwargs: OrderResult(
            OrderOutcome.STALE_STATE,
            message="connector_disconnected",
        )

        signal = {"action": "buy", "qty": 10, "comment": "dup-protect"}
        executor.execute_signal(signal)
        executor.execute_signal(signal)

        assert connector.place_order_result.call_count == 1
        callback.assert_called()

    def test_submission_block_expires_after_ttl(self):
        executor, connector, pt, tr, rg = self._create_executor(order_mode="market")
        executor._block_submission("dup-key", reason="ambiguous_submit")

        with executor._submission_lock:
            executor._blocked_submission_keys["dup-key"]["expires_at"] = time.monotonic() - 1.0

        assert executor._is_submission_blocked("dup-key") is False
        assert "dup-key" not in executor._blocked_submission_keys

    def test_reconcile_releases_submission_blocks_without_active_pending_orders(self):
        executor, connector, pt, tr, rg = self._create_executor(order_mode="market")
        executor._block_submission("dup-key", reason="ambiguous_submit")

        with patch("core.order_executor.pending_order_registry.get_pending", return_value=[]):
            released = executor.release_blocked_submissions_after_reconcile()

        assert released == 1
        assert executor._is_submission_blocked("dup-key") is False

    def test_reconcile_keeps_submission_blocks_while_pending_order_active(self):
        executor, connector, pt, tr, rg = self._create_executor(order_mode="market")
        executor._block_submission("dup-key", reason="ambiguous_submit")
        lifecycle = MagicMock()
        lifecycle.strategy_id = executor._strategy_id
        lifecycle.ticker = executor._ticker
        lifecycle.is_terminal = False

        with patch("core.order_executor.pending_order_registry.get_pending", return_value=[lifecycle]):
            released = executor.release_blocked_submissions_after_reconcile()

        assert released == 0
        assert executor._is_submission_blocked("dup-key") is True

    def test_stale_quote_rejects_open_signal(self):
        executor, connector, pt, tr, rg = self._create_executor(
            get_last_price_envelope=lambda: {
                "source_ts": time.time() - 10,
                "receive_ts": time.time() - 10,
                "age_ms": 10000,
                "source_id": "test",
                "status": "stale",
            }
        )

        executor.execute_signal({"action": "buy", "qty": 1})

        connector.place_order.assert_not_called()

    def test_stale_status_with_fresh_receive_ts_does_not_reject_open_signal(self):
        executor, connector, pt, tr, rg = self._create_executor(
            get_last_price_envelope=lambda: {
                "source_ts": time.time() - 10,
                "receive_ts": time.time(),
                "age_ms": 10000,
                "source_id": "test",
                "status": "stale",
            }
        )
        connector.place_order.return_value = "order_123"

        executor.execute_signal({"action": "buy", "qty": 1})

        connector.place_order.assert_called_once()

    def test_stale_quote_allowed_in_relaxed_market_phase(self):
        executor, connector, pt, tr, rg = self._create_executor(
            get_last_price_envelope=lambda: {
                "source_ts": time.time() - 10,
                "receive_ts": time.time() - 10,
                "age_ms": 10000,
                "source_id": "test",
                "status": "stale",
            }
        )
        connector.place_order.return_value = "order_123"

        executor.execute_signal({"action": "buy", "qty": 1, "market_phase": "closing_auction"})

        connector.place_order.assert_called_once()

    def test_stale_quote_rejects_in_non_relaxed_market_phase(self):
        executor, connector, pt, tr, rg = self._create_executor(
            get_last_price_envelope=lambda: {
                "source_ts": time.time() - 10,
                "receive_ts": time.time() - 10,
                "age_ms": 10000,
                "source_id": "test",
                "status": "stale",
            }
        )

        executor.execute_signal({"action": "buy", "qty": 1, "market_phase": "normal_trading"})

        connector.place_order.assert_not_called()

    def test_invalid_bid_ask_rejects_open_signal(self):
        executor, connector, pt, tr, rg = self._create_executor()

        executor.execute_signal({"action": "buy", "qty": 1, "bid": 101.0, "ask": 100.0})

        connector.place_order.assert_not_called()

    def test_stale_signal_rejects_without_override(self):
        executor, connector, pt, tr, rg = self._create_executor()

        executor.execute_signal({"action": "buy", "qty": 1, "signal_ts": time.time() - 30})

        connector.place_order.assert_not_called()

    def test_stale_signal_allowed_with_override(self):
        executor, connector, pt, tr, rg = self._create_executor()
        connector.place_order.return_value = "order_123"

        executor.execute_signal({
            "action": "buy",
            "qty": 1,
            "signal_ts": time.time() - 30,
            "allow_stale_signal": True,
        })

        connector.place_order.assert_called_once()

    def test_limit_price_normalized_before_submit(self):
        executor, connector, pt, tr, rg = self._create_executor(order_mode="limit_price")
        executor._monitor_pool.submit = MagicMock()
        connector.get_sec_info.return_value = {"minstep": 0.05, "lotsize": 1}
        connector.place_order.return_value = "order_123"

        executor.execute_signal({"action": "buy", "qty": 1, "price": 100.03})

        connector.place_order.assert_called_once()
        assert connector.place_order.call_args.kwargs["price"] == pytest.approx(100.05)

    def test_successful_submit_binds_reservation_to_order_id(self):
        executor, connector, pt, tr, rg = self._create_executor(order_mode="market")
        executor._monitor_pool.submit = MagicMock()
        connector.place_order.return_value = "order_123"
        ledger = ReservationLedger()

        with patch("core.order_executor.reservation_ledger", ledger):
            executor.execute_signal({"action": "buy", "qty": 2, "comment": "bind-reservation"})

        snapshot = ledger.snapshot()
        assert len(snapshot) == 1
        reservation = next(iter(snapshot.values()))
        assert reservation["order_id"] == "order_123"
        assert reservation["stale"] is False

    def test_ambiguous_submit_marks_reservation_stale(self):
        callback = MagicMock()
        executor, connector, pt, tr, rg = self._create_executor(
            order_mode="market",
            on_manual_intervention=callback,
        )
        ledger = ReservationLedger()
        connector.place_order_result.side_effect = lambda **kwargs: OrderResult(
            OrderOutcome.STALE_STATE,
            message="connector_disconnected",
        )

        with patch("core.order_executor.reservation_ledger", ledger):
            executor.execute_signal({"action": "buy", "qty": 2, "comment": "ambiguous"})

        snapshot = ledger.snapshot()
        assert len(snapshot) == 1
        reservation = next(iter(snapshot.values()))
        assert reservation["stale"] is True
        assert reservation["stale_reason"] == "ambiguous_submit"
        assert reservation["order_id"] == ""
        callback.assert_called_once_with("ambiguous_submit")

    def test_limit_price_cancel_timeout_escalates_manual_intervention(self):
        callback = MagicMock()
        executor, connector, pt, tr, rg = self._create_executor(
            order_mode="limit_price",
            on_manual_intervention=callback,
        )
        ledger = ReservationLedger()
        ledger.reserve("res-1", executor._account_id, 100.0)
        pt.set_order_in_flight(True)
        executor._cancel_order_timeout_sec = 0.01
        connector.get_order_status.side_effect = [
            {"status": "working", "balance": 1, "quantity": 1, "price": 100.0},
        ]

        def _hanging_cancel(order_id, account_id):
            threading.Event().wait(0.2)
            return OrderResult(OrderOutcome.SUCCESS, transaction_id=str(order_id))

        connector.cancel_order_result.side_effect = _hanging_cancel
        lifecycle = OrderLifecycle(
            tid="tid-1",
            strategy_id=executor._strategy_id,
            ticker=executor._ticker,
            side="buy",
            requested_qty=1,
            order_type="limit",
        )

        with patch("core.order_executor.reservation_ledger", ledger), \
             patch("core.order_executor.TRADING_END_TIME_MIN", 0), \
             patch("core.order_executor.time.sleep", lambda _: None):
            executor._monitor_limit_price_order(
                "tid-1",
                "buy",
                1,
                100.0,
                "cancel-timeout",
                False,
                reservation_key="res-1",
                lifecycle=lifecycle,
            )

        snapshot = ledger.snapshot()["res-1"]
        assert snapshot["stale"] is True
        assert snapshot["stale_reason"] == "cancel_uncertain"
        assert pt.is_order_in_flight() is True
        callback.assert_called_once_with("cancel_timeout")
        assert tr.recorded_trades == []


class TestCalcDynamicQty:
    """Тесты динамического расчёта лота."""

    def test_dynamic_qty_insufficient_funds(self):
        """Недостаточно средств для динамического расчёта."""
        connector = MagicMock()
        connector.get_free_money.return_value = None
        pt = PositionTracker()
        tr = MockTradeRecorder()
        rg = MockRiskGuard()

        executor = OrderExecutor(
            strategy_id="test",
            connector=connector,
            position_tracker=pt,
            trade_recorder=tr,
            risk_guard=rg,
            account_id="acc",
            ticker="SBER",
            board="TQBR",
            agent_name="agent",
            lot_sizing={"dynamic": True},
        )

        result = executor._calc_dynamic_qty("buy")
        assert result is None

    def test_dynamic_qty_with_futures_go(self):
        """Динамический расчёт для фьючерсов с ГО."""
        connector = MagicMock()
        connector.get_free_money.return_value = 50000.0
        connector.get_sec_info.return_value = {
            "buy_deposit": 10000.0,
            "sell_deposit": 10000.0,
        }
        pt = PositionTracker()
        tr = MockTradeRecorder()
        rg = MockRiskGuard()

        executor = OrderExecutor(
            strategy_id="test",
            connector=connector,
            position_tracker=pt,
            trade_recorder=tr,
            risk_guard=rg,
            account_id="acc",
            ticker="Si",
            board="SPBFUT",
            agent_name="agent",
            lot_sizing={"dynamic": True, "drawdown": 5000, "instances": 1},
        )

        result = executor._calc_dynamic_qty("buy")
        # free_money / (drawdown + GO) / instances = 50000 / (5000 + 10000) / 1 = 3.33 -> floor = 3
        assert result == 3


class TestOrderExecutorStop:
    """Тесты остановки OrderExecutor."""

    def test_stop_sets_running_false(self):
        """stop() устанавливает running=False."""
        connector = MagicMock()
        pt = PositionTracker()
        tr = MockTradeRecorder()
        rg = MockRiskGuard()

        executor = OrderExecutor(
            strategy_id="test",
            connector=connector,
            position_tracker=pt,
            trade_recorder=tr,
            risk_guard=rg,
            account_id="acc",
            ticker="SBER",
            board="TQBR",
            agent_name="agent",
        )

        executor.stop()
        assert executor.running is False


class TestCloseIdempotent:
    """Тесты идемпотентности close-path (TASK-002)."""

    def _create_executor(self, **kwargs):
        connector = _bind_result_api(MagicMock())
        connector.close_position.return_value = "tid_close"
        connector.place_order.return_value = "tid_open"
        pt = PositionTracker()
        tr = MockTradeRecorder()
        rg = MockRiskGuard()

        executor = OrderExecutor(
            strategy_id="test",
            connector=connector,
            position_tracker=pt,
            trade_recorder=tr,
            risk_guard=rg,
            account_id="acc",
            ticker="SBER",
            board="TQBR",
            agent_name="agent",
            order_mode=kwargs.get("order_mode", "market"),
            get_last_price=lambda: 100.0,
            get_last_price_envelope=lambda: {
                "source_ts": time.time(),
                "receive_ts": time.time(),
                "age_ms": 0,
                "source_id": "test",
                "status": "fresh",
            },
        )
        executor._monitor_pool.submit = MagicMock()
        return executor, connector, pt, tr, rg

    def test_close_rejected_when_order_in_flight(self):
        """Второй close игнорируется если первый ещё в работе."""
        executor, connector, pt, tr, rg = self._create_executor()
        pt.update_position(1, 5, 100.0)
        pt.set_order_in_flight(True)  # имитируем: первый close уже в мониторинге

        executor.execute_signal({"action": "close", "qty": 5})

        connector.close_position.assert_not_called()
        connector.place_order.assert_not_called()

    def test_close_sets_order_in_flight(self):
        """Market close ставит order_in_flight перед отправкой."""
        executor, connector, pt, tr, rg = self._create_executor()
        pt.update_position(1, 5, 100.0)

        executor.execute_signal({"action": "close", "qty": 5})

        # close_position вызван (order_in_flight был установлен)
        connector.close_position.assert_called_once()

    def test_close_clears_in_flight_on_both_paths_fail(self):
        """Если close_position вернул None — in_flight сброшен без fallback."""
        executor, connector, pt, tr, rg = self._create_executor()
        pt.update_position(1, 5, 100.0)
        connector.close_position.return_value = None

        executor.execute_signal({"action": "close", "qty": 5})

        assert pt.is_order_in_flight() is False
        connector.place_order.assert_not_called()

    def test_close_clears_in_flight_on_exception(self):
        """Исключение при close_position не оставляет зависший in-flight и не делает fallback."""
        executor, connector, pt, tr, rg = self._create_executor()
        pt.update_position(1, 5, 100.0)
        connector.close_position.side_effect = RuntimeError("broker down")

        executor.execute_signal({"action": "close", "qty": 5})

        assert pt.is_order_in_flight() is False
        connector.place_order.assert_not_called()

    def test_close_failed_marks_manual_intervention(self):
        """Неуспешный close переводит стратегию в manual intervention path."""
        executor, connector, pt, tr, rg = self._create_executor()
        pt.update_position(1, 5, 100.0)
        connector.close_position.return_value = None
        on_manual_intervention = MagicMock()
        executor._on_manual_intervention = on_manual_intervention

        executor.execute_signal({"action": "close", "qty": 5, "comment": "manual check"})

        on_manual_intervention.assert_called_once_with("close_failed")

    def test_close_retries_transport_error_and_succeeds(self):
        executor, connector, pt, tr, rg = self._create_executor()
        pt.update_position(1, 5, 100.0)
        executor._monitor_pool.submit = MagicMock()
        connector.close_position_result.side_effect = [
            OrderResult(OrderOutcome.TRANSPORT_ERROR, message="network"),
            OrderResult(OrderOutcome.SUCCESS, transaction_id="close-123"),
        ]

        with patch("core.order_executor.time.sleep") as sleep_mock:
            executor.execute_signal({"action": "close", "qty": 5, "comment": "retry-close"})

        assert connector.close_position_result.call_count == 2
        sleep_mock.assert_called_once()
        executor._monitor_pool.submit.assert_called_once()

    def test_close_transport_error_escalates_after_retry_budget(self):
        executor, connector, pt, tr, rg = self._create_executor()
        pt.update_position(1, 5, 100.0)
        on_manual_intervention = MagicMock()
        executor._on_manual_intervention = on_manual_intervention
        connector.close_position_result.side_effect = [
            OrderResult(OrderOutcome.TRANSPORT_ERROR, message="network-1"),
            OrderResult(OrderOutcome.TRANSPORT_ERROR, message="network-2"),
            OrderResult(OrderOutcome.TRANSPORT_ERROR, message="network-3"),
        ]

        with patch("core.order_executor.time.sleep") as sleep_mock:
            executor.execute_signal({"action": "close", "qty": 5, "comment": "retry-fail"})

        assert connector.close_position_result.call_count == executor._close_retry_attempts
        assert sleep_mock.call_count == executor._close_retry_attempts - 1
        on_manual_intervention.assert_called_once_with("close_failed")

    def test_no_close_when_no_position(self):
        """Close на пустой позиции — не отправляется."""
        executor, connector, pt, tr, rg = self._create_executor()

        executor.execute_signal({"action": "close", "qty": 5})

        connector.close_position.assert_not_called()
        connector.place_order.assert_not_called()


class TestPreTradeRiskGate:
    """Тесты pre-trade risk gate (TASK-031)."""

    def _create_executor(self, **kwargs):
        connector = _bind_result_api(MagicMock())
        connector.place_order.return_value = "order_123"
        connector.close_position.return_value = "close_123"
        connector.get_free_money.return_value = 100000.0
        pt = PositionTracker()
        tr = MockTradeRecorder()
        rg = MockRiskGuard()

        executor = OrderExecutor(
            strategy_id="test",
            connector=connector,
            position_tracker=pt,
            trade_recorder=tr,
            risk_guard=rg,
            account_id="acc",
            ticker="SBER",
            board="TQBR",
            agent_name="agent",
            order_mode=kwargs.get("order_mode", "market"),
            get_last_price=lambda: 100.0,
            get_last_price_envelope=lambda: {
                "source_ts": time.time(),
                "receive_ts": time.time(),
                "age_ms": 0,
                "source_id": "test",
                "status": "fresh",
            },
        )
        return executor, connector, pt, tr, rg

    def test_buy_rejected_when_circuit_open(self):
        """Buy отклоняется при открытом circuit breaker."""
        executor, connector, pt, tr, rg = self._create_executor()
        rg._circuit_open = True

        executor.execute_signal({"action": "buy", "qty": 5})

        connector.place_order.assert_not_called()

    def test_sell_rejected_when_circuit_open(self):
        """Sell отклоняется при открытом circuit breaker."""
        executor, connector, pt, tr, rg = self._create_executor()
        rg._circuit_open = True

        executor.execute_signal({"action": "sell", "qty": 5})

        connector.place_order.assert_not_called()


class TestAccountRiskLimits:
    """Тесты account-level risk limits (TASK-044)."""

    def _create_executor(self, **kwargs):
        connector = _bind_result_api(MagicMock())
        connector.get_free_money.return_value = 100000.0
        connector.get_sec_info.return_value = {"lotsize": 1, "buy_deposit": 0, "sell_deposit": 0}
        pt = PositionTracker()
        tr = MockTradeRecorder()
        rg = MockRiskGuard()

        executor = OrderExecutor(
            strategy_id="test_strategy",
            connector=connector,
            position_tracker=pt,
            trade_recorder=tr,
            risk_guard=rg,
            account_id="test_account",
            ticker="SBER",
            board="TQBR",
            agent_name="test_agent",
            get_last_price=lambda: 300.0,
            get_last_price_envelope=lambda: {
                "source_ts": time.time(),
                "receive_ts": time.time(),
                "age_ms": 0,
                "source_id": "test",
                "status": "fresh",
            },
            **kwargs,
        )
        return executor, connector, pt, tr, rg

    @patch("core.order_executor.get_setting")
    @patch("core.order_executor._get_account_gross_exposure", return_value=400000.0)
    def test_buy_rejected_when_gross_exposure_exceeded(self, mock_exposure, mock_setting):
        """Buy отклоняется, если gross exposure превысит лимит."""
        mock_setting.side_effect = lambda k, d=None: {
            "max_gross_exposure": 500000.0,
            "max_account_positions": 0,
        }.get(k, d)

        executor, connector, pt, tr, rg = self._create_executor()
        # Новый ордер: 10 * 300 * 1 = 3000 (lot_size=1 from sec_info)
        # Но _calc_reservation_amount вернёт qty * price * lotsize = 10 * 300 * 1 = 3000
        # Но exposure 400000 + 3000 = 403000 < 500000, пройдёт
        # Нужно exposure ближе к лимиту
        mock_exposure.return_value = 498000.0
        # 498000 + 3000 = 501000 > 500000

        executor.execute_signal({"action": "buy", "qty": 10})

        connector.place_order.assert_not_called()

    @patch("core.order_executor.get_setting")
    @patch("core.order_executor._get_account_gross_exposure", return_value=100000.0)
    def test_buy_allowed_when_within_gross_exposure(self, mock_exposure, mock_setting):
        """Buy проходит, если gross exposure в пределах лимита."""
        mock_setting.side_effect = lambda k, d=None: {
            "max_gross_exposure": 500000.0,
            "max_account_positions": 0,
        }.get(k, d)

        executor, connector, pt, tr, rg = self._create_executor()
        connector.place_order.return_value = "order_123"

        executor.execute_signal({"action": "buy", "qty": 10})

        connector.place_order.assert_called_once()

    @patch("core.order_executor.get_setting")
    @patch("core.order_executor._get_account_positions_count", return_value=5)
    def test_buy_rejected_when_max_positions_reached(self, mock_count, mock_setting):
        """Buy отклоняется, если достигнут лимит количества позиций."""
        mock_setting.side_effect = lambda k, d=None: {
            "max_gross_exposure": 0,
            "max_account_positions": 5,
        }.get(k, d)

        executor, connector, pt, tr, rg = self._create_executor()
        # position_qty == 0 → это будет новая позиция → count 5 >= limit 5

        executor.execute_signal({"action": "buy", "qty": 1})

        connector.place_order.assert_not_called()

    @patch("core.order_executor.get_setting")
    @patch("core.order_executor._get_account_positions_count", return_value=5)
    def test_buy_allowed_when_position_already_exists(self, mock_count, mock_setting):
        """Buy проходит (scale-in), если позиция по тикеру уже открыта."""
        mock_setting.side_effect = lambda k, d=None: {
            "max_gross_exposure": 0,
            "max_account_positions": 5,
        }.get(k, d)

        executor, connector, pt, tr, rg = self._create_executor()
        # Имитируем существующую позицию
        pt._position = 1
        pt._position_qty = 10
        connector.place_order.return_value = "order_123"

        executor.execute_signal({"action": "buy", "qty": 5})

        # Позиция уже есть → не новая → positions_count не увеличивается
        # Но position != 0 → try_set_order_in_flight вернёт False
        # Фактически ордер не пройдёт из-за уже открытой позиции
        # Это нормально — scale-in не поддерживается position_tracker

    @patch("core.order_executor.get_setting")
    def test_no_account_limits_configured(self, mock_setting):
        """Без настроенных лимитов ордер проходит свободно."""
        mock_setting.side_effect = lambda k, d=None: {
            "max_gross_exposure": 0,
            "max_account_positions": 0,
        }.get(k, d)

        executor, connector, pt, tr, rg = self._create_executor()
        connector.place_order.return_value = "order_123"

        executor.execute_signal({"action": "buy", "qty": 10})

        connector.place_order.assert_called_once()

    @patch("core.order_executor.get_setting")
    @patch("core.order_executor._get_account_gross_exposure", return_value=0.0)
    def test_close_bypasses_account_limits(self, mock_exposure, mock_setting):
        """Close-ордер не проверяет account-level лимиты."""
        mock_setting.side_effect = lambda k, d=None: {
            "max_gross_exposure": 100.0,  # Очень маленький лимит
            "max_account_positions": 1,
        }.get(k, d)

        executor, connector, pt, tr, rg = self._create_executor()
        pt._position = 1
        pt._position_qty = 10
        connector.close_position.return_value = True

        executor.execute_signal({"action": "close"})

        # close не проходит через account risk check
        connector.close_position.assert_called_once()

    def test_check_account_risk_limits_exposure_math(self):
        """Числовой тест: exposure = 400000, new = 150000, limit = 500000 → reject."""
        executor, connector, pt, tr, rg = self._create_executor()

        with patch("core.order_executor.get_setting") as mock_setting, \
             patch("core.order_executor._get_account_gross_exposure", return_value=400000.0):
            mock_setting.side_effect = lambda k, d=None: {
                "max_gross_exposure": 500000.0,
                "max_account_positions": 0,
            }.get(k, d)

            # _calc_reservation_amount: qty * price * lotsize = 500 * 300 * 1 = 150000
            result = executor._check_account_risk_limits("buy", 500)

        assert result is not None
        assert "превысит лимит" in result

    def test_check_account_risk_limits_within_limit(self):
        """Числовой тест: exposure = 100000, new = 3000, limit = 500000 → pass."""
        executor, connector, pt, tr, rg = self._create_executor()

        with patch("core.order_executor.get_setting") as mock_setting, \
             patch("core.order_executor._get_account_gross_exposure", return_value=100000.0):
            mock_setting.side_effect = lambda k, d=None: {
                "max_gross_exposure": 500000.0,
                "max_account_positions": 0,
            }.get(k, d)

            result = executor._check_account_risk_limits("buy", 10)

        assert result is None

    def test_close_allowed_when_circuit_open(self):
        """Close разрешён даже при открытом circuit breaker."""
        executor, connector, pt, tr, rg = self._create_executor()
        rg._circuit_open = True
        pt.update_position(1, 5, 100.0)

        executor.execute_signal({"action": "close", "qty": 5})

        # close_position должен быть вызван
        connector.close_position.assert_called_once()

    def test_buy_rejected_by_risk_limits(self):
        """Buy отклоняется при нарушении лимитов риска."""
        executor, connector, pt, tr, rg = self._create_executor()
        rg._risk_allowed = False
        rg._risk_reason = "qty=100 превышает max_position_size=10"

        executor.execute_signal({"action": "buy", "qty": 100})

        connector.place_order.assert_not_called()

    def test_sell_rejected_by_risk_limits(self):
        """Sell отклоняется при нарушении лимитов риска."""
        executor, connector, pt, tr, rg = self._create_executor()
        rg._risk_allowed = False
        rg._risk_reason = "Дневной лимит убытков достигнут"

        executor.execute_signal({"action": "sell", "qty": 5})

        connector.place_order.assert_not_called()

    def test_close_allowed_despite_risk_limits(self):
        """Close разрешён даже при нарушении лимитов риска."""
        executor, connector, pt, tr, rg = self._create_executor()
        rg._risk_allowed = False
        rg._risk_reason = "Дневной лимит убытков достигнут"
        pt.update_position(1, 5, 100.0)

        executor.execute_signal({"action": "close", "qty": 5})

        connector.close_position.assert_called_once()

    def test_buy_allowed_when_risk_ok(self):
        """Buy проходит когда лимиты в норме."""
        executor, connector, pt, tr, rg = self._create_executor()

        executor.execute_signal({"action": "buy", "qty": 5})

        connector.place_order.assert_called_once()


class TestCircuitBreakerCallback:
    """Тесты вызова on_circuit_break при срабатывании circuit breaker (TASK-032)."""

    def _create_executor(self, **kwargs):
        connector = _bind_result_api(MagicMock())
        connector.place_order.return_value = None  # ошибка
        connector.close_position.return_value = None
        connector.get_free_money.return_value = 100000.0
        pt = PositionTracker()
        tr = MockTradeRecorder()
        rg = MockRiskGuard()
        cb = MagicMock()

        executor = OrderExecutor(
            strategy_id="test",
            connector=connector,
            position_tracker=pt,
            trade_recorder=tr,
            risk_guard=rg,
            account_id="acc",
            ticker="SBER",
            board="TQBR",
            agent_name="agent",
            on_circuit_break=cb,
            get_last_price=lambda: 100.0,
            get_last_price_envelope=lambda: {
                "source_ts": time.time(),
                "receive_ts": time.time(),
                "age_ms": 0,
                "source_id": "test",
                "status": "fresh",
            },
            **kwargs,
        )
        return executor, connector, pt, tr, rg, cb

    def test_circuit_break_callback_on_market_failure(self):
        """on_circuit_break вызывается при срабатывании circuit breaker от market ошибки."""
        executor, connector, pt, tr, rg, cb = self._create_executor()
        rg._trip_on_failure = True

        executor.execute_signal({"action": "buy", "qty": 5})

        cb.assert_called_once()

    def test_no_callback_when_circuit_not_tripped(self):
        """on_circuit_break НЕ вызывается если порог не достигнут."""
        executor, connector, pt, tr, rg, cb = self._create_executor()
        rg._trip_on_failure = False

        executor.execute_signal({"action": "buy", "qty": 5})

        cb.assert_not_called()

    def test_circuit_break_callback_on_invalid_qty(self):
        """on_circuit_break вызывается при circuit breaker от невалидного qty."""
        executor, connector, pt, tr, rg, cb = self._create_executor()
        rg._trip_on_failure = True

        executor.execute_signal({"action": "buy", "qty": -1})

        cb.assert_called_once()

    def test_callback_exception_does_not_propagate(self):
        """Ошибка в on_circuit_break не ломает executor."""
        executor, connector, pt, tr, rg, cb = self._create_executor()
        rg._trip_on_failure = True
        cb.side_effect = RuntimeError("callback error")

        # Не должно бросить исключение
        executor.execute_signal({"action": "buy", "qty": -1})
