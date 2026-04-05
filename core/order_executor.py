# core/order_executor.py

"""
Исполнитель ордеров.

Инкапсулирует логику размещения, мониторинга и завершения ордеров:
- рыночные ордера (открытие/закрытие)
- лимитные ордера по фиксированной цене
- chase-ордера (лимитка по стакану)
- мониторинг исполнения
- динамический расчёт лота
"""

import math
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional, Callable

from loguru import logger

from core.base_connector import Action, Side, OrderMode

from config.settings import TRADING_END_TIME_MIN
from core.chase_order import ChaseOrder
from core.order_lifecycle import OrderLifecycle, OrderState, pending_order_registry
from core.reservation_ledger import reservation_ledger
from core.storage import get_setting
from core.telegram_bot import notifier, EventCode


def _get_account_gross_exposure(account_id: str) -> float:
    """Рассчитывает суммарную gross-экспозицию по всем позициям счёта.

    Включает pending-резервации из reservation_ledger.
    """
    from core.position_manager import position_manager

    total = 0.0
    for pos in position_manager.get_positions(account_id):
        qty = abs(float(pos.get("quantity", 0)))
        price = float(pos.get("current_price", 0) or pos.get("avg_price", 0))
        lot_size = int(pos.get("lot_size", 1) or 1)
        if qty > 0 and price > 0:
            total += qty * price * lot_size

    total += reservation_ledger.total_reserved(account_id)
    return total


def _get_account_positions_count(account_id: str) -> int:
    """Подсчитывает количество открытых позиций на счёте."""
    from core.position_manager import position_manager

    count = 0
    for pos in position_manager.get_positions(account_id):
        qty = float(pos.get("quantity", 0))
        if qty != 0:
            count += 1
    return count


class OrderExecutor:
    """Исполнитель ордеров для одной стратегии/тикета.

    Зависимости:
        connector: коннектор к бирже
        position_tracker: трекер позиции
        trade_recorder: регистратор сделок
        risk_guard: circuit breaker
    """

    def __init__(
        self,
        strategy_id: str,
        connector,
        position_tracker,
        trade_recorder,
        risk_guard,
        account_id: str,
        ticker: str,
        board: str,
        agent_name: str,
        order_mode: OrderMode = "market",
        lot_sizing: dict = None,
        get_last_price: Callable = None,
        get_point_cost: Callable = None,
        get_lot_size: Callable = None,
        is_futures: Callable = None,
        calculate_commission: Callable = None,
        on_reconcile: Callable = None,
        on_circuit_break: Callable = None,
    ):
        self._strategy_id = strategy_id
        self._connector = connector
        self._position_tracker = position_tracker
        self._trade_recorder = trade_recorder
        self._risk_guard = risk_guard
        self._account_id = account_id
        self._ticker = ticker
        self._board = board
        self._agent_name = agent_name
        self._order_mode = order_mode
        self._lot_sizing = lot_sizing or {}
        self._get_last_price = get_last_price or (lambda: 0.0)
        self._get_point_cost = get_point_cost or (lambda: 1.0)
        self._get_lot_size = get_lot_size or (lambda: 1)
        self._is_futures = is_futures or (lambda: False)
        self._calculate_commission = calculate_commission
        self._on_reconcile = on_reconcile
        self._on_circuit_break = on_circuit_break

        self._chase_lock = threading.Lock()
        self._active_chase_orders: list = []
        self._running = True
        self._monitor_pool = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix=f"order-monitor-{strategy_id}"
        )
        self._monitor_pool_closed = False
        self._monitor_pool_finalizer = weakref.finalize(
            self, OrderExecutor._shutdown_executor, self._monitor_pool
        )

        # Таймауты
        self._market_timeout_sec = 45

        # Текущий reservation key (для отмены при ошибке до submit)
        self._reservation_counter = 0
        self._reservation_counter_lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._running

    @staticmethod
    def _shutdown_executor(executor: ThreadPoolExecutor):
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)
        except Exception:
            pass

    def _cleanup_monitor_pool(self):
        if self._monitor_pool_closed:
            return
        self._monitor_pool_closed = True
        self._shutdown_executor(self._monitor_pool)
        if hasattr(self, "_monitor_pool_finalizer") and self._monitor_pool_finalizer.alive:
            self._monitor_pool_finalizer.detach()

    def _account_has_position(self, ticker: str) -> bool:
        from core.position_manager import position_manager

        for pos in position_manager.get_positions(self._account_id):
            if pos.get("ticker") != ticker:
                continue
            qty = float(pos.get("quantity", 0) or 0)
            if qty != 0:
                return True
        return False

    def _check_account_risk_limits(
        self,
        action: str,
        qty: int,
        ticker: str = "",
        board: str = "",
        last_price: float = 0.0,
    ) -> Optional[str]:
        """Проверяет account-level risk limits (cross-strategy).

        Returns:
            None — ордер разрешён.
            str  — причина отклонения.
        """
        ticker = ticker or self._ticker
        board = board or self._board
        max_gross = float(get_setting("max_gross_exposure", 0) or 0)
        max_positions = int(float(get_setting("max_account_positions", 0) or 0))

        if max_gross <= 0 and max_positions <= 0:
            return None  # лимиты не настроены

        if max_gross > 0:
            current_exposure = _get_account_gross_exposure(self._account_id)
            new_order_cost = self._calc_reservation_amount(
                action, qty, ticker=ticker, board=board, last_price=last_price
            )
            total = current_exposure + new_order_cost
            if total > max_gross:
                return (
                    f"gross_exposure {total:.2f} превысит лимит "
                    f"{max_gross:.2f} (текущая={current_exposure:.2f}, "
                    f"новый ордер={new_order_cost:.2f})"
                )

        if max_positions > 0:
            current_count = _get_account_positions_count(self._account_id)
            if not self._account_has_position(ticker):
                if current_count >= max_positions:
                    return (
                        f"количество позиций {current_count} достигло лимита "
                        f"{max_positions}"
                    )

        return None

    def _next_reservation_key(self) -> str:
        with self._reservation_counter_lock:
            self._reservation_counter += 1
            return f"{self._strategy_id}:{self._ticker}:{self._reservation_counter}"

    def _calc_reservation_amount(
        self,
        side: str,
        qty: int,
        ticker: str = "",
        board: str = "",
        last_price: float = 0.0,
    ) -> float:
        """Рассчитывает сумму капитала, которую заблокирует ордер."""
        ticker = ticker or self._ticker
        board = board or self._board
        sec_info = None
        if hasattr(self._connector, "get_sec_info"):
            sec_info = self._connector.get_sec_info(ticker, board)

        go = 0.0
        if sec_info:
            go = float(
                sec_info.get("buy_deposit" if side == "buy" else "sell_deposit") or 0
            )

        if go > 0:
            return qty * go

        price = last_price or 0.0
        if price <= 0:
            if ticker == self._ticker and board == self._board:
                price = self._get_last_price()
            elif hasattr(self._connector, "get_last_price"):
                try:
                    price = self._connector.get_last_price(ticker, board) or 0.0
                except Exception:
                    price = 0.0
        lot_size = int(sec_info.get("lotsize", 1)) if sec_info else 1
        return qty * price * lot_size

    def check_account_risk_limits_for_order(
        self,
        action: str,
        qty: int,
        ticker: str = "",
        board: str = "",
        last_price: float = 0.0,
    ) -> Optional[str]:
        return self._check_account_risk_limits(
            action=action,
            qty=qty,
            ticker=ticker,
            board=board,
            last_price=last_price,
        )

    def reserve_capital_for_order(
        self,
        action: str,
        qty: int,
        ticker: str = "",
        board: str = "",
        last_price: float = 0.0,
    ) -> str:
        reservation_key = self._next_reservation_key()
        reservation_amount = self._calc_reservation_amount(
            action,
            qty,
            ticker=ticker,
            board=board,
            last_price=last_price,
        )
        if reservation_amount > 0:
            reservation_ledger.reserve(reservation_key, self._account_id, reservation_amount)
            return reservation_key
        return ""

    def release_reserved_capital(self, reservation_key: str):
        if reservation_key:
            reservation_ledger.release(reservation_key)

    def stop(self):
        """Остановить executor и отменить активные chase-ордера."""
        self._running = False
        self._cleanup_monitor_pool()
        with self._chase_lock:
            active_chases = list(self._active_chase_orders)
            self._active_chase_orders.clear()

        for chase in active_chases:
            if not chase.is_done:
                chase.cancel()
                chase.wait(timeout=5)

    def _handle_failure(self):
        """Регистрирует ошибку и вызывает on_circuit_break при срабатывании."""
        if self._risk_guard.record_failure():
            logger.error(
                f"[{self._strategy_id}] CIRCUIT BREAKER: "
                f"порог ошибок достигнут — вызов аварийного обработчика"
            )
            if self._on_circuit_break:
                try:
                    self._on_circuit_break()
                except Exception as e:
                    logger.error(
                        f"[{self._strategy_id}] Ошибка в on_circuit_break: {e}"
                    )

    # --- Публичные методы исполнения ---

    def execute_signal(self, signal: dict):
        """Исполняет торговый сигнал.

        order_mode='market'      — рыночная заявка.
        order_mode='limit'       — лимитка по лучшей цене в стакане (ChaseOrder).
        order_mode='limit_price' — лимитка по цене из сигнала (signal["price"]).
        """
        action = signal.get("action")
        qty = signal.get("qty", 1)

        # Валидация qty
        try:
            qty = int(qty)
            if qty <= 0:
                logger.error(
                    f"[{self._strategy_id}] Некорректный qty={qty} в сигнале, должен быть > 0"
                )
                self._handle_failure()
                return
        except (TypeError, ValueError):
            logger.error(
                f"[{self._strategy_id}] Некорректный тип qty={qty} в сигнале, ожидается число"
            )
            self._handle_failure()
            return

        comment = signal.get("comment", "")

        # Динамический лот
        if action in ("buy", "sell") and self._lot_sizing.get("dynamic"):
            dyn_qty = self._calc_dynamic_qty(action)
            if dyn_qty is not None:
                qty = dyn_qty
                logger.info(f"[{self._strategy_id}] Динамический лот: {qty}")
            else:
                logger.warning(
                    f"[{self._strategy_id}] Недостаточно средств для {action} "
                    f"(свободных средств: {self._connector.get_free_money(self._account_id)}), "
                    f"сигнал пропущен"
                )
                return

        fill_price = self._get_last_price()
        fill_price_text = f"{fill_price:.4f}" if fill_price else "н/д"

        # === Pre-trade risk gate ===
        if action in ("buy", "sell"):
            # Circuit breaker — запрещаем открывающие ордера
            if self._risk_guard.is_circuit_open():
                logger.warning(
                    f"[{self._strategy_id}] RISK REJECT: circuit breaker открыт, "
                    f"{action.upper()} x{qty} цена~{fill_price_text} отклонён"
                )
                return

            # Лимиты риска (max_position_size, daily_loss_limit)
            allowed, reason = self._risk_guard.check_risk_limits(action, qty)
            if not allowed:
                logger.warning(
                    f"[{self._strategy_id}] RISK REJECT: {reason}, "
                    f"{action.upper()} x{qty} цена~{fill_price_text} отклонён"
                )
                return

            # Account-level risk limits (cross-strategy)
            account_reject = self._check_account_risk_limits(action, qty)
            if account_reject:
                logger.warning(
                    f"[{self._strategy_id}] ACCOUNT RISK REJECT: {account_reject}, "
                    f"{action.upper()} x{qty} цена~{fill_price_text} отклонён"
                )
                return

        try:
            if action in ("buy", "sell"):
                # Атомарная проверка: нет позиции + нет ордера → ставим in-flight
                if not self._position_tracker.try_set_order_in_flight():
                    state = self._position_tracker.get_state()
                    if state["position"] != 0:
                        logger.warning(
                            f"[{self._strategy_id}] Позиция уже открыта "
                            f"({state['position']}, qty={state['position_qty']}), "
                            f"игнорируем {action.upper()} цена~{fill_price_text}"
                        )
                    else:
                        logger.warning(
                            f"[{self._strategy_id}] Ордер уже в работе, "
                            f"игнорируем {action.upper()} цена~{fill_price_text}"
                        )
                    return

                # Резервируем капитал под pending-ордер
                res_key = self._next_reservation_key()
                res_amount = self._calc_reservation_amount(action, qty)
                if res_amount > 0:
                    reservation_ledger.reserve(res_key, self._account_id, res_amount)

                if self._order_mode == "limit":
                    self._execute_chase(action, qty, comment, reservation_key=res_key)
                elif self._order_mode == "limit_price":
                    price = float(signal.get("price", 0)) or fill_price
                    self._execute_limit_price(action, qty, comment, price, reservation_key=res_key)
                else:
                    self._execute_market(action, qty, comment, fill_price, reservation_key=res_key)

            elif action == "close":
                if self._order_mode in ("limit", "limit_price"):
                    # Атомарная проверка: есть позиция + нет ордера → ставим in-flight
                    if not self._position_tracker.try_set_order_in_flight_for_close():
                        state = self._position_tracker.get_state()
                        if state["position"] == 0:
                            logger.warning(
                                f"[{self._strategy_id}] Нет открытой позиции, "
                                f"игнорируем CLOSE цена~{fill_price_text}"
                            )
                        else:
                            logger.warning(
                                f"[{self._strategy_id}] Лимитный ордер уже в работе, "
                                f"игнорируем CLOSE цена~{fill_price_text}"
                            )
                        return
                else:
                    # Market-mode: тоже используем guarded check
                    if not self._position_tracker.try_set_order_in_flight_for_close():
                        state = self._position_tracker.get_state()
                        if state["position"] == 0:
                            logger.warning(
                                f"[{self._strategy_id}] Нет открытой позиции, "
                                f"игнорируем CLOSE цена~{fill_price_text}"
                            )
                        else:
                            logger.warning(
                                f"[{self._strategy_id}] Ордер уже в работе, "
                                f"игнорируем CLOSE цена~{fill_price_text}"
                            )
                        return

                pos = self._position_tracker.get_position()
                close_side = "sell" if pos == 1 else "buy"
                close_qty = abs(self._position_tracker.get_position_qty())

                if self._order_mode == "limit":
                    self._execute_chase(close_side, close_qty, comment, is_close=True)
                elif self._order_mode == "limit_price":
                    price = float(signal.get("price", 0)) or fill_price
                    self._execute_limit_price(
                        close_side, close_qty, comment, price, is_close=True
                    )
                else:
                    self._execute_market_close(close_side, close_qty, comment, fill_price)

        except Exception as e:
            self._position_tracker.clear_order_in_flight()
            if action in ("buy", "sell"):
                reservation_ledger.release(res_key)
            logger.error(f"[{self._strategy_id}] Ошибка исполнения {action}: {e}")
            import traceback
            logger.error(traceback.format_exc())

    # --- Рыночные ордера ---

    def _execute_market(self, side: str, qty: int, comment: str, fill_price: float,
                        reservation_key: str = ""):
        """Рыночная заявка на открытие позиции."""
        tid = self._connector.place_order(
            account_id=self._account_id,
            ticker=self._ticker,
            side=side,
            quantity=qty,
            order_type="market",
            board=self._board,
            agent_name=self._agent_name,
        )
        if tid:
            self._risk_guard.record_success()
            logger.info(
                f"[{self._strategy_id}] MARKET {side.upper()} x{qty} "
                f"@ {fill_price:.4f} tid={tid} (мониторинг...)"
            )
            self._monitor_pool.submit(
                self._monitor_market_order,
                tid, side, qty, fill_price, comment, False, reservation_key,
            )
        else:
            self._handle_failure()
            logger.error(
                f"[{self._strategy_id}] ОШИБКА заявки: "
                f"сторона={side.upper()} qty={qty} цена={fill_price} вид=market | {comment}"
            )
            self._position_tracker.clear_order_in_flight()
            reservation_ledger.release(reservation_key)

    def _execute_market_close(
        self, close_side: str, close_qty: int, comment: str, fill_price: float
    ):
        """Рыночное закрытие позиции.

        Использует connector.close_position() как каноническую точку входа.
        Если close_position вернул None (позиция не найдена брокером),
        делает fallback на place_order с явными side/qty.
        """
        tid = None
        try:
            tid = self._connector.close_position(
                account_id=self._account_id,
                ticker=self._ticker,
                quantity=close_qty,
                agent_name=self._agent_name,
            )
        except Exception as e:
            logger.warning(
                f"[{self._strategy_id}] ошибка закрытия позиции: {e}, "
                f"цена~{fill_price:.4f}"
            )
            tid = None

        if tid:
            self._risk_guard.record_success()
            logger.info(
                f"[{self._strategy_id}] CLOSE MARKET {close_side.upper()} x{close_qty} "
                f"@ {fill_price:.4f} tid={tid} (мониторинг...)"
            )
            self._monitor_pool.submit(
                self._monitor_market_order,
                tid, close_side, close_qty, fill_price, comment, True,
            )
            return

        # Fallback: позиция не найдена брокером — закрываем через place_order
        tid = self._connector.place_order(
            account_id=self._account_id,
            ticker=self._ticker,
            side=close_side,
            quantity=close_qty,
            order_type="market",
            board=self._board,
            agent_name=self._agent_name,
        )
        if tid:
            self._risk_guard.record_success()
            logger.info(
                f"[{self._strategy_id}] CLOSE MARKET {close_side.upper()} x{close_qty} "
                f"@ {fill_price:.4f} tid={tid} (fallback place_order, мониторинг...)"
            )
            self._monitor_pool.submit(
                self._monitor_market_order,
                tid, close_side, close_qty, fill_price, comment, True,
            )
        else:
            self._handle_failure()
            logger.error(
                f"[{self._strategy_id}] ОШИБКА заявки: "
                f"сторона={close_side.upper()} qty={close_qty} "
                f"цена={fill_price} вид=market(close) | {comment}"
            )
            self._position_tracker.clear_order_in_flight()

    def _monitor_market_order(
        self, tid: str, side: str, qty: int, price: float, comment: str, is_close: bool,
        reservation_key: str = "",
    ) -> bool:
        """Мониторинг рыночного ордера до подтверждения исполнения."""
        _TERMINAL = {
            "matched", "cancelled", "canceled", "denied", "removed", "expired", "killed"
        }
        TIMEOUT_SEC = self._market_timeout_sec

        lifecycle = OrderLifecycle(
            tid=str(tid),
            strategy_id=self._strategy_id,
            ticker=self._ticker,
            side=side,
            requested_qty=qty,
            order_type="market",
        )

        logger.debug(
            f"[{self._strategy_id}] Мониторинг MARKET tid={tid} "
            f"{side.upper()} x{qty} @ {price:.4f}"
        )

        confirmed = False
        deadline = time.monotonic() + TIMEOUT_SEC
        timeout_reached = False

        while self._running and time.monotonic() < deadline:
            try:
                info = self._connector.get_order_status(tid)
            except Exception as e:
                logger.warning(f"[{self._strategy_id}] get_order_status tid={tid}: {e}")
                info = None

            if info:
                status = info.get("status", "")
                balance = info.get("balance")
                quantity_field = info.get("quantity")
                avg_price = info.get("avg_price") or info.get("price")

                filled = 0
                if balance is not None and quantity_field is not None:
                    filled = int(quantity_field) - int(balance)

                try:
                    avg_p = float(avg_price) if avg_price else 0.0
                except (TypeError, ValueError):
                    avg_p = 0.0

                lifecycle.update_from_connector(status, filled, avg_p)

                if avg_p > 0:
                    price = avg_p

                if status == "matched":
                    confirmed = True
                    logger.info(
                        f"[{self._strategy_id}] MARKET tid={tid} "
                        f"исполнен filled={lifecycle.filled_qty}/{qty} @ {price:.4f}"
                    )
                    break
                elif status in _TERMINAL:
                    logger.info(
                        f"[{self._strategy_id}] MARKET tid={tid} завершён "
                        f"статус={status} filled={lifecycle.filled_qty}/{qty} @ {price:.4f}"
                    )
                    break

            time.sleep(0.5)

        if not confirmed and time.monotonic() >= deadline:
            timeout_reached = True
            lifecycle.mark_timeout()

        filled = lifecycle.filled_qty
        trade_to_record = None
        partial_trade = None
        notify_payload = None

        with self._position_tracker._position_lock:
            if filled > 0 and confirmed:
                self._position_tracker.close_position(filled, qty) if is_close else None
                if not is_close:
                    self._position_tracker.confirm_open(side, filled, price)
                trade_to_record = (side, filled, price, comment, str(tid))
                notify_payload = (side, filled, qty, price, comment)
                self._position_tracker.clear_order_in_flight()
                success = True
            else:
                if filled > 0:
                    if is_close:
                        self._position_tracker.close_position(filled, qty)
                    else:
                        self._position_tracker.confirm_open(side, filled, price)
                    partial_trade = (side, filled, price, comment, str(tid))
                self._position_tracker.clear_order_in_flight()
                success = False

        # Освобождаем резерв капитала (ордер завершён: fill/cancel/timeout)
        if reservation_key:
            reservation_ledger.release(reservation_key)

        if trade_to_record:
            side_r, filled_r, price_r, comment_r, tid_r = trade_to_record
            self._trade_recorder.record_trade(
                side_r, filled_r, price_r, comment_r, order_type="market", order_ref=tid_r
            )
            logger.info(
                f"[{self._strategy_id}] MARKET подтверждено: "
                f"{side_r.upper()} filled={filled_r}/{qty} @ {price_r}"
            )
            if notify_payload:
                try:
                    notifier.send(
                        EventCode.ORDER_FILLED,
                        agent=self._strategy_id,
                        description=(
                            f"{notify_payload[0].upper()} {self._ticker} "
                            f"x{notify_payload[1]} @ {notify_payload[3]} "
                            f"[market] | {notify_payload[4]}"
                        ),
                    )
                except Exception:
                    pass
            return True

        if partial_trade:
            side_p, filled_p, price_p, comment_p, tid_p = partial_trade
            self._trade_recorder.record_trade(
                side_p, filled_p, price_p, comment_p, order_type="market", order_ref=tid_p
            )
            logger.info(
                f"[{self._strategy_id}] MARKET частично: "
                f"{side_p.upper()} filled={filled_p}/{qty} @ {price_p:.4f}"
            )
            # Регистрируем для post-exit проверки на late fills
            pending_order_registry.register(lifecycle)
            return True

        if timeout_reached:
            logger.warning(
                f"[{self._strategy_id}] MARKET tid={tid} таймаут {TIMEOUT_SEC} сек, reconcile..."
            )
            # Регистрируем для post-exit проверки на late fills
            pending_order_registry.register(lifecycle)
            if self._on_reconcile:
                try:
                    self._on_reconcile()
                except Exception as e:
                    logger.warning(f"[{self._strategy_id}] reconcile error: {e}")
        return success

    # --- Лимитные ордера по фиксированной цене ---

    def _execute_limit_price(
        self, side: str, qty: int, comment: str, price: float, is_close: bool = False,
        reservation_key: str = "",
    ):
        """Лимитная заявка по фиксированной цене из сигнала."""
        tid = self._connector.place_order(
            account_id=self._account_id,
            ticker=self._ticker,
            side=side,
            quantity=qty,
            order_type="limit",
            price=price,
            board=self._board,
            agent_name=self._agent_name,
        )
        if not tid:
            logger.error(
                f"[{self._strategy_id}] ОШИБКА заявки: "
                f"сторона={side.upper()} qty={qty} цена={price} вид=limit_price | {comment}"
            )
            self._handle_failure()
            self._position_tracker.clear_order_in_flight()
            reservation_ledger.release(reservation_key)
            return

        logger.info(
            f"[{self._strategy_id}] LIMIT {side.upper()} x{qty} @ {price} tid={tid} ({comment})"
        )

        self._monitor_pool.submit(
            self._monitor_limit_price_order,
            tid, side, qty, price, comment, is_close, reservation_key,
        )

    def _monitor_limit_price_order(
        self, tid: str, side: str, qty: int, price: float, comment: str, is_close: bool,
        reservation_key: str = "",
    ):
        """Фоновый мониторинг лимитной заявки по фиксированной цене."""
        _TERMINAL = {
            "matched", "cancelled", "canceled", "denied", "removed", "expired", "killed"
        }
        CANCEL_TIME_MIN = TRADING_END_TIME_MIN

        lifecycle = OrderLifecycle(
            tid=str(tid),
            strategy_id=self._strategy_id,
            ticker=self._ticker,
            side=side,
            requested_qty=qty,
            order_type="limit",
        )

        cancelled_by_time = False

        logger.debug(
            f"[{self._strategy_id}] Мониторинг LIMIT tid={tid} "
            f"{side.upper()} x{qty} @ {price}"
        )

        while self._running:
            try:
                info = self._connector.get_order_status(tid)
            except Exception as e:
                logger.warning(f"[{self._strategy_id}] get_order_status tid={tid}: {e}")
                info = None

            if info:
                status = info.get("status", "")
                balance = info.get("balance")
                quantity_field = info.get("quantity")

                filled = 0
                if balance is not None and quantity_field is not None:
                    filled = int(quantity_field) - int(balance)

                avg_price = info.get("avg_price") or info.get("price") or 0.0
                try:
                    avg_p = float(avg_price)
                except (TypeError, ValueError):
                    avg_p = 0.0

                lifecycle.update_from_connector(status, filled, avg_p)

                if status in _TERMINAL:
                    logger.info(
                        f"[{self._strategy_id}] LIMIT tid={tid} "
                        f"статус={status} filled={lifecycle.filled_qty}/{qty}"
                    )
                    break

            now_min = datetime.now().hour * 60 + datetime.now().minute
            if now_min >= CANCEL_TIME_MIN:
                logger.info(
                    f"[{self._strategy_id}] LIMIT tid={tid} "
                    f"снимается по времени 23:45 (filled={lifecycle.filled_qty}/{qty})"
                )
                lifecycle.mark_cancel_pending()
                try:
                    self._connector.cancel_order(tid, self._account_id)
                except Exception as e:
                    logger.warning(f"[{self._strategy_id}] cancel_order tid={tid}: {e}")
                cancelled_by_time = True
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    time.sleep(0.1)
                    try:
                        info2 = self._connector.get_order_status(tid)
                        if info2:
                            st2 = info2.get("status", "")
                            b2 = info2.get("balance")
                            q2 = info2.get("quantity")
                            avg2 = info2.get("avg_price") or info2.get("price") or 0.0
                            filled2 = 0
                            if b2 is not None and q2 is not None:
                                filled2 = int(q2) - int(b2)
                            try:
                                avg2 = float(avg2)
                            except (TypeError, ValueError):
                                avg2 = 0.0
                            lifecycle.update_from_connector(st2, filled2, avg2)
                            if st2 in _TERMINAL:
                                break
                    except Exception:
                        pass
                break

            time.sleep(1.0)

        filled = lifecycle.filled_qty

        with self._position_tracker._position_lock:
            self._position_tracker.clear_order_in_flight()
            if filled > 0:
                if is_close:
                    self._position_tracker.close_position(filled, qty)
                else:
                    self._position_tracker.confirm_open(side, filled, price)

                self._trade_recorder.record_trade(
                    side, filled, price, comment, order_type="limit", order_ref=str(tid)
                )
                logger.info(
                    f"[{self._strategy_id}] LIMIT исполнена: "
                    f"{side.upper()} filled={filled}/{qty} @ {price} "
                    f"{'(снята по времени, частично)' if cancelled_by_time and filled < qty else ''}"
                )

                try:
                    notifier.send(
                        EventCode.ORDER_FILLED,
                        agent=self._strategy_id,
                        description=(
                            f"{side.upper()} {self._ticker} x{filled} @ {price} "
                            f"[limit_price] | {comment}"
                        ),
                    )
                except Exception:
                    pass

                # Регистрируем для late fill проверки если partial
                if filled < qty:
                    pending_order_registry.register(lifecycle)
            else:
                if cancelled_by_time:
                    logger.info(
                        f"[{self._strategy_id}] LIMIT tid={tid} снята в 23:45, не исполнена"
                    )
                else:
                    logger.warning(
                        f"[{self._strategy_id}] LIMIT tid={tid} завершена без исполнения"
                    )

        # Освобождаем резерв капитала
        if reservation_key:
            reservation_ledger.release(reservation_key)

    # --- Chase-ордера ---

    def _execute_chase(self, side: str, qty: int, comment: str, is_close: bool = False,
                       reservation_key: str = ""):
        """Лимитная заявка через ChaseOrder (стакан)."""
        import time as _time
        chase_ref = (
            f"chase:{self._strategy_id}:{self._ticker}:{side}:{int(_time.time() * 1000)}"
        )

        chase_price = self._get_last_price() or 0.0
        chase_price_text = f"{chase_price:.4f}" if chase_price else "bid/offer"
        logger.info(
            f"[{self._strategy_id}] Chase {side.upper()} x{qty} "
            f"цена~{chase_price_text} ({comment}) — фоновый поток"
        )

        chase = ChaseOrder(
            connector=self._connector,
            account_id=self._account_id,
            ticker=self._ticker,
            side=side,
            quantity=qty,
            board=self._board,
            agent_name=self._agent_name,
        )

        with self._chase_lock:
            self._active_chase_orders.append(chase)

        def _run():
            try:
                chase.wait(timeout=120)

                filled_qty = chase.filled_qty
                target_qty = qty
                fill_rate = (filled_qty / target_qty * 100) if target_qty > 0 else 0

                if fill_rate < 50:
                    logger.warning(
                        f"[{self._strategy_id}] Частичное исполнение: "
                        f"{filled_qty}/{target_qty} ({fill_rate:.1f}%) "
                        f"цена~{chase.avg_price:.4f}"
                    )
                elif fill_rate < 100:
                    logger.info(
                        f"[{self._strategy_id}] Неполное исполнение: "
                        f"{filled_qty}/{target_qty} ({fill_rate:.1f}%) "
                        f"цена~{chase.avg_price:.4f}"
                    )
            finally:
                with self._chase_lock:
                    if chase in self._active_chase_orders:
                        self._active_chase_orders.remove(chase)

                if not chase.is_done:
                    chase.cancel()

                self._on_chase_done(chase, side, qty, comment, is_close, chase_ref,
                                    reservation_key)

        self._monitor_pool.submit(_run)

    def _on_chase_done(
        self, chase, side: str, qty: int, comment: str, is_close: bool, chase_ref: str = "",
        reservation_key: str = "",
    ):
        """Вызывается из фонового потока после завершения ChaseOrder."""
        if not self._running:
            logger.warning(
                f"[{self._strategy_id}] _on_chase_done пропущен — engine остановлен"
            )
            if reservation_key:
                reservation_ledger.release(reservation_key)
            return

        filled = chase.filled_qty
        avg_px = chase.avg_price

        if filled <= 0:
            logger.error(
                f"[{self._strategy_id}] ОШИБКА заявки: "
                f"сторона={side.upper()} qty={qty} цена=bid/offer "
                f"вид=limit(стакан) — ничего не исполнено за 60 сек | {comment}"
            )
            self._handle_failure()
            self._position_tracker.clear_order_in_flight()
            if reservation_key:
                reservation_ledger.release(reservation_key)
            return

        if is_close:
            self._position_tracker.close_position(filled, qty)
        else:
            self._position_tracker.confirm_open(side, filled, avg_px)

        self._position_tracker.clear_order_in_flight()

        # Освобождаем резерв капитала
        if reservation_key:
            reservation_ledger.release(reservation_key)

        logger.info(
            f"[{self._strategy_id}] Запись chase-ордера в history: "
            f"exec_key={chase_ref}, side={side}, filled={filled}, avg_px={avg_px}"
        )
        self._trade_recorder.record_trade(
            side, filled, avg_px, comment, order_type="chase", order_ref=chase_ref
        )
        self._risk_guard.record_success()

        logger.info(
            f"[{self._strategy_id}] Chase done: {side.upper()} "
            f"filled={filled}/{qty} avg={avg_px:.4f} ({comment})"
        )

        try:
            notifier.send(
                EventCode.ORDER_FILLED,
                agent=self._strategy_id,
                description=(
                    f"{side.upper()} {self._ticker} x{filled} @ {avg_px:.4f} "
                    f"[chase] | {comment}"
                ),
            )
        except Exception:
            pass

    # --- Динамический расчёт лота ---

    def _calc_dynamic_qty(self, side: str) -> Optional[int]:
        """Рассчитывает динамический лот.

        Формула: Floor((available_money / (drawdown + GO)) / instances)

        available_money = free_money - уже зарезервированный капитал
        по другим pending-ордерам на этом же account_id.
        """
        free_money = self._connector.get_free_money(self._account_id)
        if free_money is None or free_money <= 0:
            return None

        available_money = reservation_ledger.available(self._account_id, free_money)
        if available_money <= 0:
            logger.debug(
                f"[{self._strategy_id}] dynamic qty: free={free_money:.2f} "
                f"reserved={free_money - available_money:.2f} available=0"
            )
            return None

        sec_info = None
        if hasattr(self._connector, "get_sec_info"):
            sec_info = self._connector.get_sec_info(self._ticker, self._board)

        go = 0.0
        if sec_info:
            go = float(
                sec_info.get("buy_deposit" if side == "buy" else "sell_deposit") or 0
            )

        manual_dd = float(self._lot_sizing.get("drawdown", 0))
        strat_dd = 0  # get_max_drawdown вызывается извне
        effective_dd = max(manual_dd, strat_dd)

        instances = max(int(self._lot_sizing.get("instances", 1)), 1)

        if go <= 0:
            price = self._get_last_price()
            if price <= 0:
                return int(self._lot_sizing.get("lot", 1)) or 1

            lot_size = int(sec_info.get("lotsize", 1)) if sec_info else 1
            position_cost = price * lot_size

            if position_cost <= 0:
                return int(self._lot_sizing.get("lot", 1)) or 1

            qty = math.floor(available_money / position_cost / instances)
            return qty if qty >= 1 else None

        denom = effective_dd + go
        if denom <= 0:
            return None

        qty = math.floor((available_money / denom) / instances)
        return qty if qty >= 1 else None

    def check_pending_late_fills(self) -> list[dict]:
        """Проверяет pending ордера на late fills.

        Вызывается из reconcile path или dedicated checker.
        Late fills записываются через TradeRecorder как repair event.

        Returns:
            Список обнаруженных late fills.
        """
        late_fills = pending_order_registry.check_late_fills(self._connector)

        for lf in late_fills:
            if lf["strategy_id"] != self._strategy_id:
                continue

            delta = lf["delta"]
            side = lf["side"]
            avg_price = lf["avg_price"]
            tid = lf["tid"]

            logger.warning(
                f"[{self._strategy_id}] LATE FILL REPAIR: "
                f"tid={tid} {side.upper()} +{delta} fills @ {avg_price:.4f}"
            )

            # Записываем дополнительные fills как repair event
            repair_ref = f"late_fill:{tid}:{delta}"
            try:
                self._trade_recorder.record_trade(
                    side, delta, avg_price,
                    f"late_fill_repair tid={tid}",
                    order_type="market",
                    order_ref=repair_ref,
                )
            except Exception as e:
                logger.error(
                    f"[{self._strategy_id}] LATE FILL REPAIR failed: {e}"
                )

            # Убираем из реестра после обработки
            pending_order_registry.unregister(tid)

        return late_fills
