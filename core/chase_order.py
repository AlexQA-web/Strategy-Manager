# core/chase_order.py
#
# Роль: Фоновый daemon-поток для исполнения лимитной заявки по лучшей цене стакана.
# Логика: Ставит лимитку по bid (buy) или offer (sell), следит за изменением лучшей цены,
#         переставляет заявку до полного исполнения всего объёма.
# Вызов: LiveEngine._execute_chase() создаёт экземпляр и вызывает wait().
# Потребители: LiveEngine (реальная торговля).
#
# Особенности:
#   - _total_qty фиксируется при создании и не меняется — это эталон для учёта остатка.
#   - _filled_qty накапливается атомарно через _lock.
#   - remaining_qty = _total_qty - _filled_qty — остаток для следующей заявки.
#   - При отмене текущего ордера: ждём финального статуса (matched/cancelled/...) перед
#     отпиской watcher'а, чтобы не потерять частичные fills в момент отмены.
#   - После cancel_order() делаем poll статуса до 2 сек — защита от race condition.
#   - Если place_order вернул None — ждём 1 сек и повторяем (не теряем остаток).

import threading
import time
from typing import Optional

from loguru import logger


# Финальные статусы ордера (исполнен, снят, отклонён)
_TERMINAL_STATUSES = {"matched", "cancelled", "canceled", "denied", "removed", "expired", "killed"}


class ChaseOrder:
    """
    Фоновый daemon-поток: ставит лимитку по лучшей цене в стакане
    и автоматически перемещает при изменении лучшей цены.

    Гарантирует: filled_qty + remaining_qty == total_qty в любой момент.
    """

    def __init__(self, connector, account_id: str, ticker: str, side: str,
                 quantity: int, board: str = "TQBR", agent_name: str = ""):
        self._connector = connector
        self._account_id = account_id
        self._ticker = ticker
        self._side = side  # "buy" | "sell"
        self._board = board
        self._agent_name = agent_name

        self._total_qty = quantity       # эталон — не меняется
        self._filled_qty = 0             # накопленный объём исполненных лотов
        self._fill_cost = 0.0            # sum(price * qty) для avg_price

        self._current_tid: Optional[str] = None   # transactionid текущей заявки
        self._current_price: Optional[float] = None
        self._cancel_requested = threading.Event()
        self._done_event = threading.Event()

        self._lock = threading.Lock()

        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"chase-{ticker}-{side}")
        self._thread.start()

    # ── Публичный API ────────────────────────────────────────────────────

    def cancel(self):
        """Запросить отмену chase-ордера."""
        self._cancel_requested.set()

    @property
    def is_done(self) -> bool:
        return self._done_event.is_set()

    @property
    def filled_qty(self) -> int:
        with self._lock:
            return self._filled_qty

    @property
    def avg_price(self) -> float:
        with self._lock:
            return (self._fill_cost / self._filled_qty) if self._filled_qty > 0 else 0.0

    @property
    def remaining_qty(self) -> int:
        with self._lock:
            return self._total_qty - self._filled_qty

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Блокирующее ожидание завершения. Возвращает True если завершён."""
        return self._done_event.wait(timeout=timeout)

    # ── Внутренние методы ────────────────────────────────────────────────

    def _track_order_fills(self, tid: str, price: float):
        """
        Подписывается на ордер через watch_order.
        Возвращает watcher-функцию (нужна для последующей отписки).

        Важно: watcher захватывает price через замыкание — это цена конкретной
        заявки, не self._current_price (которая может измениться при переставлении).
        """
        order_filled_in_this_leg = 0

        def watcher(t, info):
            nonlocal order_filled_in_this_leg
            balance = info.get("balance")
            quantity = info.get("quantity")

            if balance is not None and quantity is not None:
                new_fill = int(quantity) - int(balance)
                if new_fill > order_filled_in_this_leg:
                    delta = new_fill - order_filled_in_this_leg
                    order_filled_in_this_leg = new_fill
                    with self._lock:
                        self._filled_qty += delta
                        self._fill_cost += delta * price
                    logger.debug(
                        f"[Chase] {self._ticker} partial fill +{delta} @ {price}, "
                        f"total filled={self._filled_qty}/{self._total_qty}"
                    )

        self._connector.watch_order(tid, watcher)
        return watcher

    def _wait_for_terminal_status(self, tid: str, timeout: float = 2.0) -> str:
        """
        Поллит статус ордера до получения финального или истечения timeout.
        Возвращает последний известный статус.
        Нужен после cancel_order() чтобы дождаться финальных fills.
        """
        deadline = time.monotonic() + timeout
        delay = 0.05  # начинаем с 50ms
        while time.monotonic() < deadline:
            try:
                info = self._connector.get_order_status(tid)
                if info:
                    status = info.get("status", "")
                    if status in _TERMINAL_STATUSES:
                        return status
            except Exception:
                pass
            time.sleep(delay)
            delay = min(delay * 1.5, 0.3)  # backoff до 300ms
        return "unknown"

    def _get_target_price(self) -> Optional[float]:
        """Лучшая цена: bid для buy, offer для sell.

        Пассивная лимитка (maker): BUY по bid, SELL по offer.
        Fallback: если bid/offer отсутствуют — используем last из котировок
        или last_price через get_last_price (если коннектор поддерживает).
        """
        try:
            quote = self._connector.get_best_quote(self._board, self._ticker)
            if quote:
                if self._side == "buy":
                    price = quote.get("bid")
                else:
                    price = quote.get("offer")
                if price:
                    return price
                # bid/offer = 0 или None — пробуем last
                last = quote.get("last")
                if last:
                    logger.debug(
                        f"[Chase] {self._ticker} bid/offer недоступны, "
                        f"fallback last={last}"
                    )
                    return last
            # Нет котировок вообще — пробуем get_last_price
            if hasattr(self._connector, "get_last_price"):
                price = self._connector.get_last_price(self._ticker, self._board)
                if price:
                    logger.debug(
                        f"[Chase] {self._ticker} get_best_quote вернул None, "
                        f"fallback get_last_price={price}"
                    )
                    return price
            logger.warning(f"[Chase] {self._ticker} нет цены (bid/offer/last недоступны)")
            return None
        except Exception as e:
            logger.warning(f"[Chase] get_best_quote error: {e}")
            return None

    def _place(self, price: float, qty: int) -> Optional[str]:
        """Выставляет лимитную заявку. Возвращает tid или None."""
        try:
            tid = self._connector.place_order(
                account_id=self._account_id,
                ticker=self._ticker,
                side=self._side,
                quantity=qty,
                order_type="limit",
                price=price,
                board=self._board,
                agent_name=self._agent_name,
            )
            if tid:
                logger.info(f"[Chase] {self._side.upper()} {self._ticker} x{qty} @ {price} tid={tid}")
            return tid
        except Exception as e:
            logger.error(f"[Chase] place_order error: {e}")
            return None

    def _cancel_and_wait(self, tid: str, watcher) -> None:
        """
        Отменяет ордер и ждёт финального статуса (до 2 сек).
        Только после этого отписывает watcher — чтобы не потерять fills при отмене.
        """
        try:
            self._connector.cancel_order(tid, self._account_id)
            logger.debug(f"[Chase] Cancel sent tid={tid}")
        except Exception as e:
            logger.warning(f"[Chase] cancel_order error tid={tid}: {e}")

        # Ждём финального статуса — fills могут прийти ещё после cancel
        self._wait_for_terminal_status(tid, timeout=2.0)

        # Теперь безопасно отписываться
        if watcher:
            try:
                self._connector.unwatch_order(tid, watcher)
            except Exception:
                pass

    # ── Основной цикл ────────────────────────────────────────────────────

    def _run(self):
        try:
            self._connector.subscribe_quotes(self._board, self._ticker)
            # Даём время на получение первой котировки
            time.sleep(0.5)

            current_watcher = None

            while not self._cancel_requested.is_set():
                remaining = self.remaining_qty
                if remaining <= 0:
                    logger.info(
                        f"[Chase] {self._ticker} fully filled "
                        f"{self._filled_qty}/{self._total_qty} @ avg {self.avg_price:.4f}"
                    )
                    break

                target_price = self._get_target_price()
                if target_price is None:
                    # Нет котировки — ждём
                    if self._cancel_requested.wait(0.3):
                        break
                    continue

                # Нужно ли переставлять?
                if self._current_price == target_price and self._current_tid:
                    # Цена не изменилась — проверяем статус ордера
                    try:
                        status_info = self._connector.get_order_status(self._current_tid)
                    except Exception:
                        status_info = None

                    if status_info and status_info.get("status") in _TERMINAL_STATUSES:
                        # Ордер завершён — отписываем watcher и пересоздаём если остаток > 0
                        old_tid = self._current_tid
                        old_watcher = current_watcher
                        self._current_tid = None
                        self._current_price = None
                        current_watcher = None
                        if old_watcher and old_tid:
                            try:
                                self._connector.unwatch_order(old_tid, old_watcher)
                            except Exception:
                                pass
                        continue

                    if self._cancel_requested.wait(0.2):
                        break
                    continue

                # Цена изменилась или нет активного ордера — переставляем
                if self._current_tid:
                    old_tid = self._current_tid
                    old_watcher = current_watcher
                    self._current_tid = None
                    self._current_price = None
                    current_watcher = None
                    # Отменяем и ждём финальных fills перед пересозданием
                    self._cancel_and_wait(old_tid, old_watcher)

                remaining = self.remaining_qty
                if remaining <= 0:
                    break

                tid = self._place(target_price, remaining)
                if tid:
                    self._current_tid = tid
                    self._current_price = target_price
                    # Передаём цену этой конкретной заявки в watcher
                    current_watcher = self._track_order_fills(tid, target_price)
                else:
                    logger.warning(f"[Chase] Failed to place order for {self._ticker}, retry in 1s")
                    if self._cancel_requested.wait(1.0):
                        break
                    continue

                if self._cancel_requested.wait(0.2):
                    break

            # Завершение: снимаем текущую заявку если есть
            if self._current_tid:
                old_tid = self._current_tid
                old_watcher = current_watcher
                self._current_tid = None
                current_watcher = None
                self._cancel_and_wait(old_tid, old_watcher)

        except Exception as e:
            logger.error(f"[Chase] {self._ticker} error: {e}")
        finally:
            try:
                self._connector.unsubscribe_quotes(self._board, self._ticker)
            except Exception:
                pass
            self._done_event.set()
            logger.info(
                f"[Chase] {self._ticker} done: filled={self.filled_qty}/{self._total_qty} "
                f"avg={self.avg_price:.4f}"
            )
