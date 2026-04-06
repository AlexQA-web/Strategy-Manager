"""Тесты для core/order_placer.py."""

import time
import threading
from unittest.mock import MagicMock, patch

import pytest

from core.order_placer import (
    OrderPlacer,
    OrderResult,
    get_last_price,
    get_best_bid,
    get_best_offer,
    has_market_data,
)
from core.chase_order import ChaseOrder


# ── Фикстуры ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_connector():
    """Создаёт mock-коннектор с базовыми методами."""
    conn = MagicMock()
    conn.place_order.return_value = 'test-order-123'
    conn.get_order_status.return_value = {
        'status': 'matched',
        'balance': 0,
        'quantity': 1,
    }
    conn.get_best_quote.return_value = {
        'bid': 100.0,
        'offer': 101.0,
        'last': 100.5,
    }
    conn.get_last_price.return_value = 100.5
    conn.cancel_order.return_value = True
    return conn


@pytest.fixture
def placer(mock_connector):
    return OrderPlacer(mock_connector, agent_name='TestStrategy')


# ── Тесты вспомогательных функций ─────────────────────────────────────────────

class TestGetLastPrice:
    def test_returns_last_from_quote(self, mock_connector):
        mock_connector.get_best_quote.return_value = {
            'bid': 100.0, 'offer': 101.0, 'last': 100.5,
        }
        result = get_last_price(mock_connector, 'SPBFUT', 'SiH6')
        assert result == 100.5

    def test_falls_back_to_bid(self, mock_connector):
        mock_connector.get_best_quote.return_value = {
            'bid': 100.0, 'offer': 101.0, 'last': None,
        }
        result = get_last_price(mock_connector, 'SPBFUT', 'SiH6')
        assert result == 100.0

    def test_falls_back_to_offer(self, mock_connector):
        mock_connector.get_best_quote.return_value = {
            'bid': None, 'offer': 101.0, 'last': None,
        }
        result = get_last_price(mock_connector, 'SPBFUT', 'SiH6')
        assert result == 101.0

    def test_falls_back_to_get_last_price(self, mock_connector):
        mock_connector.get_best_quote.return_value = {}
        mock_connector.get_last_price.return_value = 99.0
        result = get_last_price(mock_connector, 'SPBFUT', 'SiH6')
        assert result == 99.0

    def test_returns_zero_on_failure(self, mock_connector):
        mock_connector.get_best_quote.side_effect = Exception('fail')
        mock_connector.get_last_price.side_effect = Exception('fail')
        result = get_last_price(mock_connector, 'SPBFUT', 'SiH6')
        assert result == 0.0


class TestGetBestBid:
    def test_returns_bid(self, mock_connector):
        mock_connector.get_best_quote.return_value = {'bid': 50.0}
        result = get_best_bid(mock_connector, 'TQBR', 'SBER')
        assert result == 50.0

    def test_returns_none_if_no_bid(self, mock_connector):
        mock_connector.get_best_quote.return_value = {'offer': 51.0}
        result = get_best_bid(mock_connector, 'TQBR', 'SBER')
        assert result is None


class TestGetBestOffer:
    def test_returns_offer(self, mock_connector):
        mock_connector.get_best_quote.return_value = {'offer': 51.0}
        result = get_best_offer(mock_connector, 'TQBR', 'SBER')
        assert result == 51.0

    def test_returns_none_if_no_offer(self, mock_connector):
        mock_connector.get_best_quote.return_value = {'bid': 50.0}
        result = get_best_offer(mock_connector, 'TQBR', 'SBER')
        assert result is None


class TestHasMarketData:
    def test_true_when_quote_available(self, mock_connector):
        assert has_market_data(mock_connector, 'SPBFUT', 'SiH6') is True

    def test_false_when_no_quote(self, mock_connector):
        mock_connector.get_best_quote.side_effect = Exception('no data')
        assert has_market_data(mock_connector, 'SPBFUT', 'SiH6') is False

    def test_false_when_empty_quote(self, mock_connector):
        mock_connector.get_best_quote.return_value = {}
        assert has_market_data(mock_connector, 'SPBFUT', 'SiH6') is False


# ── Тесты OrderPlacer.place_market ────────────────────────────────────────────

class TestPlaceMarket:
    def test_success(self, placer, mock_connector):
        result = placer.place_market('acct1', 'SPBFUT', 'SiH6', 'buy', 1)
        assert result.success is True
        assert result.order_id == 'test-order-123'
        mock_connector.place_order.assert_called_once_with(
            account_id='acct1',
            ticker='SiH6',
            side='buy',
            quantity=1,
            order_type='market',
            board='SPBFUT',
            agent_name='TestStrategy',
        )

    def test_failure_when_place_order_returns_none(self, placer, mock_connector):
        mock_connector.place_order.return_value = None
        result = placer.place_market('acct1', 'SPBFUT', 'SiH6', 'buy', 1)
        assert result.success is False
        assert 'returned None' in result.error

    def test_failure_when_place_order_returns_false(self, placer, mock_connector):
        mock_connector.place_order.return_value = False
        result = placer.place_market('acct1', 'SPBFUT', 'SiH6', 'buy', 1)
        assert result.success is False

    def test_exception_handling(self, placer, mock_connector):
        mock_connector.place_order.side_effect = RuntimeError('network error')
        result = placer.place_market('acct1', 'SPBFUT', 'SiH6', 'buy', 1)
        assert result.success is False
        assert 'network error' in result.error


# ── Тесты OrderPlacer.place_chase ─────────────────────────────────────────────

class TestPlaceChase:
    def test_starts_chase_thread(self, placer, mock_connector):
        mock_connector.get_best_quote.return_value = {
            'bid': 100.0, 'offer': 101.0, 'last': 100.5,
        }
        # watch_order сразу сообщает о полном исполнении, чтобы chase завершился
        def _watch_order_fill(tid, watcher):
            watcher(tid, {"balance": 0, "quantity": 1, "status": "matched"})
        mock_connector.watch_order.side_effect = _watch_order_fill

        result = placer.place_chase('acct1', 'SPBFUT', 'SiH6', 'buy', 1)
        assert result.success is True

    def test_fallback_to_market_when_no_data(self, placer, mock_connector):
        mock_connector.get_best_quote.side_effect = Exception('no data')
        result = placer.place_chase('acct1', 'SPBFUT', 'SiH6', 'buy', 1)
        assert result.success is True
        # Fallback должен вызвать place_market
        mock_connector.place_order.assert_called_once()
        call_kwargs = mock_connector.place_order.call_args
        assert call_kwargs.kwargs['order_type'] == 'market'

    def test_no_fallback_returns_error(self, placer, mock_connector):
        mock_connector.get_best_quote.side_effect = Exception('no data')
        result = placer.place_chase(
            'acct1', 'SPBFUT', 'SiH6', 'buy', 1,
            fallback_to_market=False,
        )
        assert result.success is False
        assert 'Нет рыночных данных' in result.error

    def test_on_failed_callback_when_chase_fails(self, placer, mock_connector):
        """Проверяет что on_failed вызывается при нулевом исполнении."""
        mock_connector.get_best_quote.return_value = {
            'bid': 100.0, 'offer': 101.0, 'last': 100.5,
        }
        # place_order возвращает None — chase не сможет исполнить
        mock_connector.place_order.return_value = None
        failed_called = threading.Event()

        def on_failed():
            failed_called.set()

        # Отключаем fallback чтобы on_failed был вызван
        result = placer.place_chase(
            'acct1', 'SPBFUT', 'SiH6', 'buy', 1,
            on_failed=on_failed,
            fallback_to_market=False,
            timeout=1.0,  # короткий timeout чтобы chase быстро завершился
        )
        # Chase запущен, ждём завершения
        assert result.success is True
        assert failed_called.wait(timeout=10), "on_failed callback was not called"

    def test_fallback_market_uses_remaining_qty_after_partial_fill(self, placer, mock_connector):
        fallback_done = threading.Event()
        filled_callbacks = []

        class FakeChaseOrder:
            def __init__(self, **kwargs):
                self._cancelled = False
                self._done = False
                self._filled_qty = 0
                self._remaining_qty = 3
                self._avg_price = 100.25

            def wait(self, timeout=None):
                if self._cancelled:
                    self._filled_qty = 2
                    self._remaining_qty = 1
                    self._done = True
                    return True
                return False

            def cancel(self):
                self._cancelled = True

            @property
            def is_done(self):
                return self._done

            @property
            def filled_qty(self):
                return self._filled_qty

            @property
            def remaining_qty(self):
                return self._remaining_qty

            @property
            def avg_price(self):
                return self._avg_price

        original_place_market = placer.place_market

        def _place_market(*args, **kwargs):
            result = original_place_market(*args, **kwargs)
            fallback_done.set()
            return result

        with patch('core.chase_order.ChaseOrder', FakeChaseOrder):
            with patch.object(placer, 'place_market', side_effect=_place_market) as market_mock:
                result = placer.place_chase(
                    'acct1', 'SPBFUT', 'SiH6', 'buy', 3,
                    timeout=0.01,
                    on_filled=lambda filled_qty, avg_price: filled_callbacks.append((filled_qty, avg_price)),
                )

                assert result.success is True
                assert fallback_done.wait(timeout=2) is True

        market_mock.assert_called_once()
        assert market_mock.call_args.args[:5] == ('acct1', 'SPBFUT', 'SiH6', 'buy', 1)
        assert filled_callbacks == [(2, 100.25)]


class TestChaseOrder:
    def test_place_order_failures_use_exponential_backoff(self, mock_connector):
        recorded_waits = []

        class _DormantThread:
            def __init__(self, target=None, daemon=None, name=None):
                self.target = target
                self.daemon = daemon
                self.name = name

            def start(self):
                return None

        mock_connector.place_order.return_value = None

        with patch('core.chase_order.threading.Thread', _DormantThread):
            chase = ChaseOrder(
                connector=mock_connector,
                account_id='acct1',
                ticker='SiH6',
                side='buy',
                quantity=1,
                board='SPBFUT',
                agent_name='TestStrategy',
            )

        def _wait(delay):
            recorded_waits.append(delay)
            return len(recorded_waits) >= 3

        with patch('core.chase_order.time.sleep', lambda _: None), \
             patch.object(chase._cancel_requested, 'wait', side_effect=_wait):
            chase._run()

        assert recorded_waits == [0.25, 0.5, 1.0]


# ── Тесты OrderPlacer.place_limit_price ───────────────────────────────────────

class TestLimitPrice:
    def test_places_limit_order(self, placer, mock_connector):
        result = placer.place_limit_price('acct1', 'SPBFUT', 'SiH6', 'buy', 1)
        assert result.success is True
        assert result.order_id == 'test-order-123'
        mock_connector.place_order.assert_called_once()
        call_kwargs = mock_connector.place_order.call_args
        assert call_kwargs.kwargs['order_type'] == 'limit'
        assert call_kwargs.kwargs['price'] == 100.5

    def test_fallback_to_market_when_no_price(self, placer, mock_connector):
        mock_connector.get_best_quote.return_value = {}
        mock_connector.get_last_price.return_value = 0.0
        result = placer.place_limit_price('acct1', 'SPBFUT', 'SiH6', 'buy', 1)
        # Fallback на market
        assert result.success is True
        calls = mock_connector.place_order.call_args_list
        assert len(calls) == 1
        assert calls[0].kwargs['order_type'] == 'market'

    def test_no_fallback_returns_error(self, placer, mock_connector):
        mock_connector.get_best_quote.return_value = {}
        mock_connector.get_last_price.return_value = 0.0
        result = placer.place_limit_price(
            'acct1', 'SPBFUT', 'SiH6', 'buy', 1,
            fallback_to_market=False,
        )
        assert result.success is False
        assert 'Нет цены' in result.error


# ── Тесты OrderPlacer.place (универсальный метод) ─────────────────────────────

class TestPlace:
    def test_market_mode(self, placer, mock_connector):
        result = placer.place('acct1', 'SPBFUT', 'SiH6', 'buy', 1, order_mode='market')
        assert result.success is True
        call_kwargs = mock_connector.place_order.call_args
        assert call_kwargs.kwargs['order_type'] == 'market'

    def test_limit_mode(self, placer, mock_connector):
        result = placer.place('acct1', 'SPBFUT', 'SiH6', 'buy', 1, order_mode='limit')
        assert result.success is True

    def test_limit_book_mode(self, placer, mock_connector):
        result = placer.place('acct1', 'SPBFUT', 'SiH6', 'buy', 1, order_mode='limit_book')
        assert result.success is True

    def test_limit_price_mode(self, placer, mock_connector):
        result = placer.place('acct1', 'SPBFUT', 'SiH6', 'buy', 1, order_mode='limit_price')
        assert result.success is True
        call_kwargs = mock_connector.place_order.call_args
        assert call_kwargs.kwargs['order_type'] == 'limit'
