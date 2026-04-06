# tests/test_reconciler.py

"""Unit-тесты для core/reconciler.py"""

import time
from unittest.mock import MagicMock, patch
import pytest

from core.reconciler import Reconciler
from core.position_tracker import PositionTracker


class TestReconcilerInit:
    """Тесты инициализации Reconciler."""

    def test_default_init(self):
        """Проверяет базовую инициализацию."""
        connector = MagicMock()
        pt = PositionTracker()

        reconciler = Reconciler(
            strategy_id="test",
            ticker="SBER",
            account_id="test_account",
            connector=connector,
            position_tracker=pt,
        )

        assert reconciler._strategy_id == "test"
        assert reconciler._ticker == "SBER"
        assert reconciler._reconcile_interval_sec == 60.0


class TestReconcile:
    """Тесты метода reconcile."""

    def _create_reconciler(self, broker_qty=0, history_qty=0, internal_qty=0):
        connector = MagicMock()
        connector.get_positions.return_value = [
            {"ticker": "SBER", "quantity": broker_qty}
        ]
        pt = PositionTracker()
        if internal_qty != 0:
            pt.update_position(1 if internal_qty > 0 else -1, internal_qty, 150.0)

        def mock_get_order_pairs(sid):
            if history_qty != 0:
                return [{"open": {"ticker": "SBER", "quantity": abs(history_qty), "side": "buy" if history_qty > 0 else "sell"}, "close": None}]
            return []

        reconciler = Reconciler(
            strategy_id="test",
            ticker="SBER",
            account_id="test_account",
            connector=connector,
            position_tracker=pt,
            get_order_pairs=mock_get_order_pairs,
            reconcile_interval_sec=0,  # Отключаем интервал для тестов
        )
        return reconciler, connector, pt

    def test_reconcile_no_mismatch(self):
        """Нет рассинхрона — всё совпадает."""
        reconciler, connector, pt = self._create_reconciler(
            broker_qty=10, history_qty=10, internal_qty=10
        )

        with patch("core.reconciler.notifier.send") as mock_send:
            result = reconciler.reconcile()

        assert result is False
        mock_send.assert_not_called()

    def test_reconcile_engine_broker_mismatch(self):
        """Рассинхрон engine vs broker."""
        reconciler, connector, pt = self._create_reconciler(
            broker_qty=15, history_qty=10, internal_qty=10
        )

        with patch("core.reconciler.notifier.send") as mock_send:
            result = reconciler.reconcile()

        assert result is True
        mock_send.assert_called_once()

    def test_reconcile_history_broker_mismatch(self):
        """Рассинхрон history vs broker."""
        reconciler, connector, pt = self._create_reconciler(
            broker_qty=5, history_qty=10, internal_qty=5
        )

        with patch("core.reconciler.notifier.send") as mock_send:
            result = reconciler.reconcile()

        assert result is True
        mock_send.assert_called_once()

    def test_reconcile_history_broker_mismatch_can_require_manual_intervention(self):
        """History divergence переводится в manual intervention вместо blind self-heal."""
        callback = MagicMock()
        reconciler, connector, pt = self._create_reconciler(
            broker_qty=5, history_qty=10, internal_qty=5
        )
        reconciler._on_history_divergence = callback
        reconciler._detect_position = MagicMock()

        with patch("core.reconciler.notifier.send"):
            result = reconciler.reconcile()

        assert result is True
        callback.assert_called_once()
        reconciler._detect_position.assert_not_called()

    def test_reconcile_skipped_if_order_in_flight(self):
        """Сверка пропускается если ордер в полёте."""
        reconciler, connector, pt = self._create_reconciler(
            broker_qty=15, history_qty=10, internal_qty=10
        )
        pt.set_order_in_flight(True)

        result = reconciler.reconcile()
        assert result is False

    def test_reconcile_respects_interval(self):
        """Сверка respects interval."""
        reconciler, connector, pt = self._create_reconciler(
            broker_qty=15, history_qty=10, internal_qty=10
        )
        # Первый вызов — сверка происходит
        reconciler.reconcile()
        # Второй вызов сразу — должен быть пропущен
        reconciler._reconcile_interval_sec = 60.0
        result = reconciler.reconcile()
        assert result is False

    def test_reconcile_result_exposes_explicit_status(self):
        reconciler, connector, pt = self._create_reconciler(
            broker_qty=10, history_qty=10, internal_qty=10
        )

        result = reconciler.reconcile_result()

        assert result.status == "ok"
        assert result.mismatch is False
        assert result.broker_qty == 10

    def test_reconcile_result_marks_history_unavailable_instead_of_fake_zero(self):
        connector = MagicMock()
        connector.get_positions.return_value = [{"ticker": "SBER", "quantity": 5}]
        pt = PositionTracker()
        pt.update_position(1, 5, 100.0)
        detect_mock = MagicMock()

        reconciler = Reconciler(
            strategy_id="test",
            ticker="SBER",
            account_id="acc",
            connector=connector,
            position_tracker=pt,
            get_order_pairs=lambda sid: (_ for _ in ()).throw(RuntimeError("history down")),
            detect_position=detect_mock,
            reconcile_interval_sec=0,
        )

        with patch("core.reconciler.notifier.send") as mock_send:
            result = reconciler.reconcile_result()

        assert result.status == "history_unavailable"
        assert result.mismatch is False
        assert result.history_qty is None
        assert result.history_error == "history down"
        detect_mock.assert_not_called()
        mock_send.assert_not_called()

    def test_self_heal_requires_mismatch_streak_threshold(self):
        reconciler, connector, pt = self._create_reconciler(
            broker_qty=15, history_qty=15, internal_qty=10
        )
        detect_mock = MagicMock()
        reconciler._detect_position = detect_mock
        reconciler._self_heal_threshold = 2
        reconciler._self_heal_cooldown_sec = 0

        with patch("core.reconciler.notifier.send"):
            reconciler.reconcile_result()
            reconciler.reconcile_result()

        assert detect_mock.call_count == 1

    def test_self_heal_respects_cooldown(self):
        reconciler, connector, pt = self._create_reconciler(
            broker_qty=15, history_qty=15, internal_qty=10
        )
        detect_mock = MagicMock()
        reconciler._detect_position = detect_mock
        reconciler._self_heal_threshold = 1
        reconciler._self_heal_cooldown_sec = 60.0

        with patch("core.reconciler.notifier.send"):
            reconciler.reconcile_result()
            reconciler.reconcile_result()

        assert detect_mock.call_count == 1

    def test_shared_position_aggregate_reconcile_uses_sum_of_strategies(self):
        connector = MagicMock()
        connector.get_positions.return_value = [{"ticker": "SBER", "quantity": 5}]
        pt = PositionTracker()
        pt.update_position(1, 3, 100.0)

        reconciler = Reconciler(
            strategy_id="sid-1",
            ticker="SBER",
            account_id="acc",
            connector=connector,
            position_tracker=pt,
            get_order_pairs=lambda sid: [
                {"open": {"ticker": "SBER", "quantity": 3, "side": "buy"}, "close": None}
            ],
            reconcile_interval_sec=0,
            allow_shared_position=True,
        )

        with patch("core.storage.get_all_strategies", return_value={
            "sid-1": {"account_id": "acc", "ticker": "SBER", "board": "TQBR"},
            "sid-2": {"account_id": "acc", "ticker": "SBER", "board": "TQBR"},
        }), patch("core.strategy_position_book.get_strategy_position_book", side_effect=[
            [{"quantity": 3, "side": "buy"}],
            [{"quantity": 2, "side": "buy"}],
        ]), patch("core.reconciler.notifier.send") as mock_send:
            result = reconciler.reconcile_result()

        assert result.status == "ok"
        assert result.mismatch is False
        assert result.aggregate_history_qty == 5
        mock_send.assert_not_called()

    def test_shared_position_aggregate_reconcile_escalates_on_sum_mismatch(self):
        connector = MagicMock()
        connector.get_positions.return_value = [{"ticker": "SBER", "quantity": 5}]
        pt = PositionTracker()
        pt.update_position(1, 3, 100.0)
        callback = MagicMock()

        reconciler = Reconciler(
            strategy_id="sid-1",
            ticker="SBER",
            account_id="acc",
            connector=connector,
            position_tracker=pt,
            get_order_pairs=lambda sid: [
                {"open": {"ticker": "SBER", "quantity": 3, "side": "buy"}, "close": None}
            ],
            reconcile_interval_sec=0,
            allow_shared_position=True,
            on_history_divergence=callback,
        )

        with patch("core.storage.get_all_strategies", return_value={
            "sid-1": {"account_id": "acc", "ticker": "SBER", "board": "TQBR"},
            "sid-2": {"account_id": "acc", "ticker": "SBER", "board": "TQBR"},
        }), patch("core.strategy_position_book.get_strategy_position_book", side_effect=[
            [{"quantity": 3, "side": "buy"}],
            [{"quantity": 1, "side": "buy"}],
        ]), patch("core.reconciler.notifier.send") as mock_send:
            result = reconciler.reconcile_result()

        assert result.status == "mismatch"
        assert result.mismatch is True
        assert result.aggregate_history_qty == 4
        callback.assert_called_once()
        mock_send.assert_called_once()


class TestGetHistoryQty:
    """Тесты получения qty из истории."""

    def test_get_history_qty_no_pairs(self):
        """Нет пар — qty = 0."""
        connector = MagicMock()
        pt = PositionTracker()

        reconciler = Reconciler(
            strategy_id="test",
            ticker="SBER",
            account_id="test_account",
            connector=connector,
            position_tracker=pt,
            get_order_pairs=lambda sid: [],
        )

        assert reconciler.get_history_qty() == 0

    def test_get_history_qty_with_open_pair(self):
        """Есть открытая пара."""
        connector = MagicMock()
        pt = PositionTracker()

        def mock_get_order_pairs(sid):
            return [
                {
                    "open": {"ticker": "SBER", "quantity": 10, "side": "buy"},
                    "close": None,
                }
            ]

        reconciler = Reconciler(
            strategy_id="test",
            ticker="SBER",
            account_id="test_account",
            connector=connector,
            position_tracker=pt,
            get_order_pairs=mock_get_order_pairs,
        )

        assert reconciler.get_history_qty() == 10

    def test_get_history_qty_with_closed_pair(self):
        """Закрытая пара не учитывается."""
        connector = MagicMock()
        pt = PositionTracker()

        def mock_get_order_pairs(sid):
            return [
                {
                    "open": {"ticker": "SBER", "quantity": 10, "side": "buy"},
                    "close": {"ticker": "SBER", "quantity": 10, "side": "sell"},
                }
            ]

        reconciler = Reconciler(
            strategy_id="test",
            ticker="SBER",
            account_id="test_account",
            connector=connector,
            position_tracker=pt,
            get_order_pairs=mock_get_order_pairs,
        )

        assert reconciler.get_history_qty() == 0

    def test_get_history_qty_wrapper_keeps_zero_compatibility_on_error(self):
        connector = MagicMock()
        pt = PositionTracker()

        reconciler = Reconciler(
            strategy_id="test",
            ticker="SBER",
            account_id="test_account",
            connector=connector,
            position_tracker=pt,
            get_order_pairs=lambda sid: (_ for _ in ()).throw(RuntimeError("history down")),
        )

        assert reconciler.get_history_qty() == 0


class TestSendAlert:
    """Тесты отправки алертов."""

    def test_send_alert_with_cooldown(self):
        """Алерт с cooldown."""
        connector = MagicMock()
        pt = PositionTracker()

        reconciler = Reconciler(
            strategy_id="test",
            ticker="SBER",
            account_id="test_account",
            connector=connector,
            position_tracker=pt,
            alert_cooldown_sec=60.0,
        )

        with patch("core.reconciler.notifier.send") as mock_send:
            reconciler.send_alert("test alert")
            assert mock_send.call_count == 1

            # Второй алерт сразу — должен быть заблокирован
            reconciler.send_alert("test alert 2")
            assert mock_send.call_count == 1


class TestBrokerUnavailable:
    """Тесты поведения при недоступности данных брокера."""

    def test_reconcile_skips_when_broker_unavailable(self):
        """Сверка пропускается если get_positions выбросил исключение."""
        connector = MagicMock()
        connector.get_positions.side_effect = Exception("connection lost")
        pt = PositionTracker()
        pt.update_position(1, 5, 100.0)

        reconciler = Reconciler(
            strategy_id="test",
            ticker="SBER",
            account_id="acc",
            connector=connector,
            position_tracker=pt,
            reconcile_interval_sec=0,
        )

        with patch("core.reconciler.notifier.send") as mock_send:
            result = reconciler.reconcile()

        assert result is False
        mock_send.assert_not_called()
        # Позиция НЕ сброшена
        assert pt.get_position_qty() == 5
        assert pt.get_position() == 1

    def test_get_broker_qty_returns_none_on_error(self):
        """_get_broker_qty возвращает None при ошибке, а не 0."""
        connector = MagicMock()
        connector.get_positions.side_effect = RuntimeError("timeout")
        pt = PositionTracker()

        reconciler = Reconciler(
            strategy_id="test",
            ticker="SBER",
            account_id="acc",
            connector=connector,
            position_tracker=pt,
        )

        assert reconciler._get_broker_qty() is None

    def test_get_broker_qty_returns_zero_for_missing_ticker(self):
        """_get_broker_qty возвращает 0 если тикер не найден (не None)."""
        connector = MagicMock()
        connector.get_positions.return_value = [
            {"ticker": "GAZP", "quantity": 10}
        ]
        pt = PositionTracker()

        reconciler = Reconciler(
            strategy_id="test",
            ticker="SBER",
            account_id="acc",
            connector=connector,
            position_tracker=pt,
        )

        assert reconciler._get_broker_qty() == 0

    def test_no_self_heal_on_broker_unavailable(self):
        """self-heal НЕ запускается при недоступности брокера."""
        connector = MagicMock()
        connector.get_positions.side_effect = Exception("timeout")
        pt = PositionTracker()
        pt.update_position(1, 5, 100.0)
        detect_mock = MagicMock()

        reconciler = Reconciler(
            strategy_id="test",
            ticker="SBER",
            account_id="acc",
            connector=connector,
            position_tracker=pt,
            detect_position=detect_mock,
            reconcile_interval_sec=0,
        )

        reconciler.reconcile()
        detect_mock.assert_not_called()
