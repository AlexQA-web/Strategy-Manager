"""
core/order_placer.py — Универсальный размещатель ордеров.

Устраняет дублирование кода _place() между стратегиями (bochka_cny.py, achilles.py).
Поддерживает три режима заявок:
  - 'market'      — рыночная заявка
  - 'limit' / 'limit_book' — лимитка по лучшей цене стакана (ChaseOrder)
  - 'limit_price' — лимитка по last price с мониторингом до TRADING_END_TIME_MIN

Использование:
    from core.order_placer import OrderPlacer

    placer = OrderPlacer(connector, agent_name='MyStrategy')

    # Рыночный ордер
    ok = placer.place_market(account_id, board, ticker, side, qty)

    # Chase-ордер (лимитка по стакану с автоперестановкой)
    ok = placer.place_chase(account_id, board, ticker, side, qty,
                            on_filled=callback, on_failed=callback)

    # Лимитка по last price
    ok = placer.place_limit_price(account_id, board, ticker, side, qty,
                                  on_filled=callback, on_failed=callback)
"""

from __future__ import annotations

import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime as _dt
from typing import Any, Callable, Optional

from loguru import logger

from core.base_connector import Side, OrderMode

# ── Константы ─────────────────────────────────────────────────────────────────

_TERMINAL_STATUSES = frozenset({
    'matched', 'cancelled', 'canceled', 'denied', 'removed', 'expired', 'killed',
})


# ── Data-классы результатов ───────────────────────────────────────────────────

@dataclass
class OrderResult:
    """Результат размещения ордера."""
    success: bool
    order_id: Optional[str] = None
    filled_qty: int = 0
    avg_price: float = 0.0
    error: str = ''


# ── Вспомогательные функции получения цен ─────────────────────────────────────

def get_last_price(connector, board: str, ticker: str) -> float:
    """Возвращает last price. Приоритет: get_best_quote → get_last_price."""
    try:
        if hasattr(connector, 'get_best_quote'):
            q = connector.get_best_quote(board, ticker)
            if q:
                last = q.get('last')
                bid = q.get('bid')
                offer = q.get('offer')
                if last and last > 0:
                    return last
                elif bid and bid > 0:
                    return bid
                elif offer and offer > 0:
                    return offer
        if hasattr(connector, 'get_last_price'):
            price = connector.get_last_price(ticker, board)
            if price and price > 0:
                return price
    except Exception as e:
        logger.warning(f'[order_placer] get_last_price {ticker}: {e}')
    return 0.0


def get_best_bid(connector, board: str, ticker: str) -> Optional[float]:
    """Возвращает лучшую цену покупки (bid)."""
    try:
        if hasattr(connector, 'get_best_quote'):
            q = connector.get_best_quote(board, ticker)
            if q:
                bid = q.get('bid')
                if bid and bid > 0:
                    return bid
    except Exception as e:
        logger.warning(f'[order_placer] get_best_bid {ticker}: {e}')
    return None


def get_best_offer(connector, board: str, ticker: str) -> Optional[float]:
    """Возвращает лучшую цену продажи (offer/ask)."""
    try:
        if hasattr(connector, 'get_best_quote'):
            q = connector.get_best_quote(board, ticker)
            if q:
                offer = q.get('offer')
                if offer and offer > 0:
                    return offer
    except Exception as e:
        logger.warning(f'[order_placer] get_best_offer {ticker}: {e}')
    return None


def has_market_data(connector, board: str, ticker: str) -> bool:
    """Проверяет доступность рыночных данных."""
    try:
        if hasattr(connector, 'get_best_quote'):
            quote = connector.get_best_quote(board, ticker)
            if quote and (quote.get('bid') or quote.get('offer') or quote.get('last')):
                return True
    except Exception:
        pass
    return False


# ── OrderPlacer ───────────────────────────────────────────────────────────────

class OrderPlacer:
    """Универсальный размещатель ордеров.

    Зависимости:
        connector — коннектор к бирже (FinamConnector, QuikConnector и т.д.)
        agent_name — имя стратегии для логирования
    """

    def __init__(self, connector, agent_name: str = 'Strategy'):
        self.connector = connector
        self.agent_name = agent_name

    # ── Публичные методы ──────────────────────────────────────────────────

    def place_market(
        self,
        account_id: str,
        board: str,
        ticker: str,
        side: Side,
        qty: int,
        comment: str = '',
    ) -> OrderResult:
        """Размещает рыночный ордер.

        Args:
            account_id: ID счёта
            board: торговая площадка (SPBFUT, TQBR и т.д.)
            ticker: тикер инструмента
            side: 'buy' или 'sell'
            qty: количество
            comment: комментарий для логирования

        Returns:
            OrderResult с результатом размещения
        """
        try:
            tid = self.connector.place_order(
                account_id=account_id,
                ticker=ticker,
                side=side,
                quantity=qty,
                order_type='market',
                board=board,
                agent_name=self.agent_name,
            )
            if tid:
                logger.info(
                    f'[{self.agent_name}] MARKET {side.upper()} {ticker}x{qty} tid={tid} | {comment}'
                )
                return OrderResult(success=True, order_id=tid)
            else:
                logger.error(
                    f'[{self.agent_name}] ОШИБКА заявки: агент={self.agent_name} '
                    f'тикер={ticker} сторона={side.upper()} qty={qty} '
                    f'вид=market — ордер не выставлен | {comment}'
                )
                return OrderResult(success=False, error='place_order returned None/False')
        except Exception as e:
            logger.error(f'[{self.agent_name}] _place {ticker} {side}: {e}')
            return OrderResult(success=False, error=str(e))

    def place_chase(
        self,
        account_id: str,
        board: str,
        ticker: str,
        side: Side,
        qty: int,
        comment: str = '',
        timeout: float = 60.0,
        on_filled: Optional[Callable[[int, float], None]] = None,
        on_failed: Optional[Callable[[], None]] = None,
        fallback_to_market: bool = True,
    ) -> OrderResult:
        """Размещает chase-ордер (лимитка по стакану с автоперестановкой).

        Если рыночные данные недоступны — fallback на market (если fallback_to_market=True).

        Args:
            account_id: ID счёта
            board: торговая площадка
            ticker: тикер
            side: 'buy' или 'sell'
            qty: количество
            comment: комментарий
            timeout: таймаут ожидания исполнения (сек)
            on_filled: callback(filled_qty, avg_price) при успешном исполнении
            on_failed: callback() при неудаче
            fallback_to_market: если True, при неудаче chase выставляет market

        Returns:
            OrderResult (success=True означает что chase запущен)
        """
        # Проверяем наличие рыночных данных
        if not has_market_data(self.connector, board, ticker):
            if fallback_to_market:
                logger.warning(
                    f'[{self.agent_name}] Нет рыночных данных для {ticker}, '
                    f'используем рыночный ордер вместо лимитного | {comment}'
                )
                return self.place_market(account_id, board, ticker, side, qty, comment)
            else:
                return OrderResult(
                    success=False,
                    error=f'Нет рыночных данных для {ticker}',
                )

        def _run_chase():
            from core.chase_order import ChaseOrder

            chase = ChaseOrder(
                connector=self.connector,
                account_id=account_id,
                ticker=ticker,
                side=side,
                quantity=qty,
                board=board,
                agent_name=self.agent_name,
            )
            chase.wait(timeout=timeout)
            if not chase.is_done:
                chase.cancel()

            if chase.filled_qty == 0:
                logger.error(
                    f'[{self.agent_name}] ОШИБКА заявки: агент={self.agent_name} '
                    f'тикер={ticker} сторона={side.upper()} qty={qty} '
                    f'цена=bid/offer вид=limit(стакан) '
                    f'— ничего не исполнено за {timeout:.0f} сек | {comment}'
                )
                if on_failed:
                    on_failed()

                # Fallback на рыночный ордер
                if fallback_to_market:
                    result = self.place_market(
                        account_id, board, ticker, side, qty,
                        comment=f'FALLBACK после chase | {comment}',
                    )
                    if result.success and on_filled:
                        on_filled(0, 0.0)  # filled_qty неизвестен здесь
            else:
                logger.info(
                    f'[{self.agent_name}] Chase {side.upper()} {ticker}x{qty} '
                    f'filled={chase.filled_qty} avg={chase.avg_price:.4f} | {comment}'
                )
                if on_filled:
                    on_filled(chase.filled_qty, chase.avg_price)

        t = threading.Thread(
            target=_run_chase, daemon=True,
            name=f'order-placer-chase-{ticker}-{side}',
        )
        t.start()
        return OrderResult(success=True)

    def place_limit_price(
        self,
        account_id: str,
        board: str,
        ticker: str,
        side: Side,
        qty: int,
        comment: str = '',
        on_filled: Optional[Callable[[int, float], None]] = None,
        on_failed: Optional[Callable[[], None]] = None,
        fallback_to_market: bool = True,
    ) -> OrderResult:
        """Размещает лимитный ордер по last price с мониторингом до 23:45.

        Args:
            account_id: ID счёта
            board: торговая площадка
            ticker: тикер
            side: 'buy' или 'sell'
            qty: количество
            comment: комментарий
            on_filled: callback(filled_qty, avg_price) при исполнении
            on_failed: callback() при неудаче
            fallback_to_market: если True, при отсутствии цены — market

        Returns:
            OrderResult (success=True означает что ордер запущен)
        """
        from config.settings import TRADING_END_TIME_MIN

        price = get_last_price(self.connector, board, ticker)
        if not price:
            if fallback_to_market:
                logger.warning(
                    f'[{self.agent_name}] limit_price: нет цены для {ticker}, fallback market'
                )
                return self.place_market(account_id, board, ticker, side, qty, comment)
            else:
                logger.warning(f'[{self.agent_name}] limit_price: нет цены для {ticker}, пропуск')
                if on_failed:
                    on_failed()
                return OrderResult(success=False, error=f'Нет цены для {ticker}')

        tid = self.connector.place_order(
            account_id=account_id,
            ticker=ticker,
            side=side,
            quantity=qty,
            order_type='limit',
            price=round(price, 6),
            board=board,
            agent_name=self.agent_name,
        )
        if not tid:
            logger.error(
                f'[{self.agent_name}] ОШИБКА заявки: агент={self.agent_name} '
                f'тикер={ticker} сторона={side.upper()} qty={qty} '
                f'цена={price:.4f} вид=limit_price — ордер не выставлен | {comment}'
            )
            if on_failed:
                on_failed()
            return OrderResult(success=False, error='place_order returned None/False')

        logger.info(
            f'[{self.agent_name}] LIMIT {side.upper()} {ticker}x{qty} '
            f'@{price:.4f} tid={tid} | {comment}'
        )

        cancel_min = TRADING_END_TIME_MIN

        def _monitor():
            filled = 0
            while True:
                try:
                    info = self.connector.get_order_status(tid)
                    if info:
                        status = info.get('status', '')
                        b = info.get('balance')
                        q = info.get('quantity')
                        if b is not None and q is not None:
                            filled = int(q) - int(b)
                        if status in _TERMINAL_STATUSES:
                            logger.info(
                                f'[{self.agent_name}] LIMIT tid={tid} {status} '
                                f'filled={filled}/{qty} | {comment}'
                            )
                            break
                except Exception as e:
                    logger.warning(f'[{self.agent_name}] monitor tid={tid}: {e}')

                now_min = _dt.now().hour * 60 + _dt.now().minute
                if now_min >= cancel_min:
                    logger.info(
                        f'[{self.agent_name}] LIMIT tid={tid} снимается в 23:45 '
                        f'(filled={filled}/{qty}) | {comment}'
                    )
                    try:
                        self.connector.cancel_order(tid, account_id)
                    except Exception:
                        pass
                    # Ждём финального статуса
                    deadline = _time.monotonic() + 2.0
                    while _time.monotonic() < deadline:
                        _time.sleep(0.1)
                        try:
                            info2 = self.connector.get_order_status(tid)
                            if info2 and info2.get('status', '') in _TERMINAL_STATUSES:
                                b2 = info2.get('balance')
                                q2 = info2.get('quantity')
                                if b2 is not None and q2 is not None:
                                    filled = int(q2) - int(b2)
                                break
                        except Exception:
                            pass
                    break
                _time.sleep(1.0)

            if filled > 0 and on_filled:
                on_filled(filled, price)

        t = threading.Thread(
            target=_monitor, daemon=True,
            name=f'order-placer-lp-{ticker}-{tid}',
        )
        t.start()
        return OrderResult(success=True, order_id=tid)

    # ── Универсальный метод place() ───────────────────────────────────────

    def place(
        self,
        account_id: str,
        board: str,
        ticker: str,
        side: Side,
        qty: int,
        order_mode: OrderMode = 'market',
        comment: str = '',
        on_filled: Optional[Callable[[int, float], None]] = None,
        on_failed: Optional[Callable[[], None]] = None,
    ) -> OrderResult:
        """Универсальный метод размещения ордера.

        Args:
            account_id: ID счёта
            board: торговая площадка
            ticker: тикер
            side: 'buy' или 'sell'
            qty: количество
            order_mode: 'market', 'limit', 'limit_book', 'limit_price'
            comment: комментарий
            on_filled: callback(filled_qty, avg_price)
            on_failed: callback()

        Returns:
            OrderResult
        """
        # Нормализуем имена режимов
        if order_mode in ('limit', 'limit_book'):
            return self.place_chase(
                account_id, board, ticker, side, qty,
                comment=comment,
                on_filled=on_filled,
                on_failed=on_failed,
            )
        elif order_mode == 'limit_price':
            return self.place_limit_price(
                account_id, board, ticker, side, qty,
                comment=comment,
                on_filled=on_filled,
                on_failed=on_failed,
            )
        else:
            return self.place_market(
                account_id, board, ticker, side, qty,
                comment=comment,
            )

    # ── Метод с callbacks для интеграции с управлением состоянием ─────────

    def place_with_state(
        self,
        account_id: str,
        board: str,
        ticker: str,
        side: str,
        qty: int,
        order_mode: str = 'market',
        comment: str = '',
        on_placed: Optional[Callable[[str], None]] = None,
        on_filled: Optional[Callable[[int, float], None]] = None,
        on_failed: Optional[Callable[[], None]] = None,
    ) -> OrderResult:
        """Размещает ордер с callbacks для интеграции с управлением состоянием.

        Используется стратегиями, которые ведут собственный учёт позиций
        (например, Achilles с pending_orders/positions).

        Args:
            account_id: ID счёта
            board: торговая площадка
            ticker: тикер
            side: 'buy' или 'sell'
            qty: количество
            order_mode: 'market', 'limit', 'limit_book', 'limit_price'
            comment: комментарий
            on_placed: callback(order_id) — вызывается при успешном размещении
            on_filled: callback(filled_qty, avg_price) — при исполнении
            on_failed: callback() — при неудаче

        Returns:
            OrderResult
        """
        if order_mode in ('limit', 'limit_book'):
            def _on_filled(filled_qty, avg_price):
                if on_filled:
                    on_filled(filled_qty, avg_price)

            def _on_failed():
                if on_failed:
                    on_failed()

            result = self.place_chase(
                account_id, board, ticker, side, qty,
                comment=comment,
                on_filled=_on_filled,
                on_failed=_on_failed,
            )
            if result.success and on_placed:
                on_placed(result.order_id or '')
            return result

        elif order_mode == 'limit_price':
            def _on_filled(filled_qty, avg_price):
                if on_filled:
                    on_filled(filled_qty, avg_price)

            def _on_failed():
                if on_failed:
                    on_failed()

            result = self.place_limit_price(
                account_id, board, ticker, side, qty,
                comment=comment,
                on_filled=_on_filled,
                on_failed=_on_failed,
            )
            if result.success and on_placed:
                on_placed(result.order_id or '')
            return result

        else:
            result = self.place_market(
                account_id, board, ticker, side, qty,
                comment=comment,
            )
            if result.success:
                if on_placed:
                    on_placed(result.order_id or '')
                # Market ордер исполняется сразу
                if on_filled:
                    on_filled(qty, 0.0)
            else:
                if on_failed:
                    on_failed()
            return result
