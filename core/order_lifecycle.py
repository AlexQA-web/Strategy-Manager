# core/order_lifecycle.py

"""
Order Lifecycle State Machine.

Описывает lifecycle ордера от размещения до terminal outcome,
включая late fills и repair path.

Состояния:
    working          — ордер активен в рынке  
    partial_fill     — частично исполнен, ещё активен
    cancel_pending   — запрошена отмена, ждём подтверждения
    matched          — полностью исполнен (terminal)
    canceled         — отменён, возможен partial fill (terminal)
    denied           — отклонён биржей/брокером (terminal)
    timeout          — мониторинг завершился по таймауту (semi-terminal)
    late_fill_repair — обнаружены дополнительные fills после terminal (repair)

Sequence rules:
    - filled_qty монотонно возрастает (никогда не уменьшается)
    - Переход в terminal состояние фиксирует финальный filled_qty
    - После terminal — возможен только переход в late_fill_repair
    - late_fill_repair фиксирует дельту между ожидаемым и фактическим filled

Потребители:
    - OrderExecutor._monitor_market_order
    - OrderExecutor._monitor_limit_price_order
    - Reconciler (для обнаружения late fills)
"""

import threading
import time
from enum import Enum
from typing import Optional

from loguru import logger

from core.storage import (
    delete_pending_order,
    get_all_pending_orders,
    save_pending_order,
)


class OrderState(str, Enum):
    """Состояния lifecycle ордера."""
    WORKING = "working"
    PARTIAL_FILL = "partial_fill"
    CANCEL_PENDING = "cancel_pending"
    MATCHED = "matched"
    CANCELED = "canceled"
    DENIED = "denied"
    TIMEOUT = "timeout"
    LATE_FILL_REPAIR = "late_fill_repair"


# Терминальные статусы — после них новые fills обрабатываются как repair
_TERMINAL_STATES = {
    OrderState.MATCHED,
    OrderState.CANCELED,
    OrderState.DENIED,
    OrderState.TIMEOUT,
    OrderState.LATE_FILL_REPAIR,
}

# Статусы коннектора → OrderState
_CONNECTOR_STATUS_MAP = {
    "matched": OrderState.MATCHED,
    "cancelled": OrderState.CANCELED,
    "canceled": OrderState.CANCELED,
    "denied": OrderState.DENIED,
    "removed": OrderState.CANCELED,
    "expired": OrderState.CANCELED,
    "killed": OrderState.DENIED,
    "working": OrderState.WORKING,
}


class OrderLifecycle:
    """Трекер lifecycle одного ордера.

    Потокобезопасен. Обеспечивает монотонность filled_qty
    и корректные переходы между состояниями.
    """

    def __init__(
        self,
        tid: str,
        strategy_id: str,
        ticker: str,
        side: str,
        requested_qty: int,
        order_type: str = "market",
        correlation_id: str = "",
    ):
        self._lock = threading.Lock()
        self.tid = tid
        self.strategy_id = strategy_id
        self.ticker = ticker
        self.side = side
        self.requested_qty = requested_qty
        self.order_type = order_type
        self.correlation_id = correlation_id

        self._state: OrderState = OrderState.WORKING
        self._filled_qty: int = 0
        self._avg_price: float = 0.0
        self._created_at: float = time.monotonic()
        self._created_at_epoch: float = time.time()
        self._terminal_at: Optional[float] = None
        self._terminal_at_epoch: Optional[float] = None
        self._terminal_filled: int = 0  # filled на момент перехода в terminal

    @classmethod
    def from_snapshot(cls, data: dict) -> "OrderLifecycle":
        lifecycle = cls(
            tid=str(data.get("tid", "")),
            strategy_id=str(data.get("strategy_id", "")),
            ticker=str(data.get("ticker", "")),
            side=str(data.get("side", "")),
            requested_qty=int(data.get("requested_qty", 0) or 0),
            order_type=str(data.get("order_type", "market") or "market"),
            correlation_id=str(data.get("correlation_id", "") or ""),
        )
        state = str(data.get("state", OrderState.WORKING.value) or OrderState.WORKING.value)
        lifecycle._state = OrderState(state)
        lifecycle._filled_qty = int(data.get("filled_qty", 0) or 0)
        lifecycle._avg_price = float(data.get("avg_price", 0.0) or 0.0)
        lifecycle._terminal_filled = int(data.get("terminal_filled", lifecycle._filled_qty) or 0)
        lifecycle._created_at_epoch = float(data.get("created_at_epoch", time.time()) or time.time())
        lifecycle._terminal_at_epoch = data.get("terminal_at_epoch")
        if lifecycle._terminal_at_epoch is not None:
            lifecycle._terminal_at_epoch = float(lifecycle._terminal_at_epoch)
            lifecycle._terminal_at = time.monotonic()
        return lifecycle

    @property
    def state(self) -> OrderState:
        with self._lock:
            return self._state

    @property
    def filled_qty(self) -> int:
        with self._lock:
            return self._filled_qty

    @property
    def avg_price(self) -> float:
        with self._lock:
            return self._avg_price

    @property
    def is_terminal(self) -> bool:
        with self._lock:
            return self._state in _TERMINAL_STATES

    @property
    def terminal_filled(self) -> int:
        """Filled на момент перехода в terminal (для late fill detection)."""
        with self._lock:
            return self._terminal_filled

    def update_from_connector(
        self,
        connector_status: str,
        filled: int,
        avg_price: float = 0.0,
    ) -> Optional[str]:
        """Обновляет состояние ордера по данным коннектора.

        Enforces:
        - Монотонность filled (не может уменьшаться)
        - Sequence rules для переходов состояний
        - Обнаружение late fills после terminal

        Args:
            connector_status: Строковый статус от коннектора.
            filled: Количество исполненных лотов.
            avg_price: Средняя цена исполнения.

        Returns:
            None — штатное обновление.
            "late_fill" — обнаружен late fill после terminal state.
            "out_of_order" — filled уменьшился (игнорировано, лог).
        """
        new_state = _CONNECTOR_STATUS_MAP.get(
            connector_status.lower(), OrderState.WORKING
        )

        with self._lock:
            # Sequence rule: filled не может уменьшаться
            if filled < self._filled_qty:
                logger.warning(
                    f"[OrderLifecycle:{self.tid}] Out-of-order: "
                    f"filled {filled} < prev {self._filled_qty}, игнорируем"
                )
                return "out_of_order"

            # Late fill detection: ордер уже в terminal, а fills увеличились
            if self._state in _TERMINAL_STATES and filled > self._filled_qty:
                delta = filled - self._filled_qty
                logger.warning(
                    f"[OrderLifecycle:{self.tid}] LATE FILL: "
                    f"+{delta} fills после {self._state.value} "
                    f"(was {self._filled_qty}, now {filled})"
                )
                self._filled_qty = filled
                if avg_price > 0:
                    self._avg_price = avg_price
                self._state = OrderState.LATE_FILL_REPAIR
                return "late_fill"

            # Обновляем filled и цену
            if filled > self._filled_qty:
                self._filled_qty = filled
            if avg_price > 0:
                self._avg_price = avg_price

            # Переход в terminal
            if new_state in _TERMINAL_STATES and self._state not in _TERMINAL_STATES:
                self._state = new_state
                self._terminal_at = time.monotonic()
                self._terminal_at_epoch = time.time()
                self._terminal_filled = self._filled_qty
                return None

            # Partial fill detection
            if self._filled_qty > 0 and self._state == OrderState.WORKING:
                if new_state == OrderState.WORKING:
                    self._state = OrderState.PARTIAL_FILL

            # Cancel pending
            if self._state not in _TERMINAL_STATES:
                if new_state == OrderState.CANCELED:
                    self._state = OrderState.CANCELED
                    self._terminal_at = time.monotonic()
                    self._terminal_at_epoch = time.time()
                    self._terminal_filled = self._filled_qty

            return None

    def mark_timeout(self) -> None:
        """Помечает ордер как завершившийся по таймауту мониторинга."""
        with self._lock:
            if self._state not in _TERMINAL_STATES:
                self._state = OrderState.TIMEOUT
                self._terminal_at = time.monotonic()
                self._terminal_at_epoch = time.time()
                self._terminal_filled = self._filled_qty

    def mark_cancel_pending(self) -> None:
        """Помечает ордер как ожидающий отмены."""
        with self._lock:
            if self._state not in _TERMINAL_STATES:
                self._state = OrderState.CANCEL_PENDING

    def get_late_fill_delta(self) -> int:
        """Возвращает кол-во late fills (разница с terminal_filled)."""
        with self._lock:
            if self._state == OrderState.LATE_FILL_REPAIR:
                return self._filled_qty - self._terminal_filled
            return 0

    def snapshot(self) -> dict:
        """Возвращает текущее состояние для отладки/логирования."""
        with self._lock:
            return {
                "tid": self.tid,
                "strategy_id": self.strategy_id,
                "ticker": self.ticker,
                "side": self.side,
                "requested_qty": self.requested_qty,
                "order_type": self.order_type,
                "correlation_id": self.correlation_id,
                "state": self._state.value,
                "filled_qty": self._filled_qty,
                "avg_price": self._avg_price,
                "terminal_filled": self._terminal_filled,
                "created_at_epoch": self._created_at_epoch,
                "terminal_at_epoch": self._terminal_at_epoch,
                "age_sec": round(max(time.time() - self._created_at_epoch, 0.0), 1),
            }


class PendingOrderRegistry:
    """Реестр ордеров, ожидающих late fill проверки.

    После выхода мониторинга из цикла ордер попадает сюда для
    post-exit проверки на late fills. Reconciler или dedicated
    checker периодически опрашивает pending_orders.
    """

    def __init__(self, max_age_sec: float = 300.0, load_from_storage: bool = True):
        self._lock = threading.Lock()
        self._orders: dict[str, OrderLifecycle] = {}
        self._max_age_sec = max_age_sec
        if load_from_storage:
            self._load_from_storage()

    def _load_from_storage(self) -> None:
        data = get_all_pending_orders()
        loaded = 0
        with self._lock:
            self._orders = {}
            for strategy_orders in data.values():
                if not isinstance(strategy_orders, dict):
                    continue
                for tid, snapshot in strategy_orders.items():
                    if not isinstance(snapshot, dict):
                        continue
                    try:
                        lifecycle = OrderLifecycle.from_snapshot(snapshot)
                        if lifecycle.tid:
                            self._orders[str(tid)] = lifecycle
                            loaded += 1
                    except Exception as exc:
                        logger.warning(
                            f"[PendingOrderRegistry] Не удалось восстановить tid={tid}: {exc}"
                        )
        if loaded:
            logger.info(f"[PendingOrderRegistry] Restored {loaded} pending orders from storage")

    def _persist_lifecycle(self, lifecycle: OrderLifecycle) -> None:
        save_pending_order(lifecycle.strategy_id, lifecycle.tid, lifecycle.snapshot())

    def register(self, lifecycle: OrderLifecycle) -> None:
        """Регистрирует ордер для post-exit мониторинга."""
        with self._lock:
            self._orders[lifecycle.tid] = lifecycle
        self._persist_lifecycle(lifecycle)

    def refresh(self, lifecycle: OrderLifecycle) -> None:
        """Обновляет durable snapshot уже зарегистрированного ордера."""
        with self._lock:
            self._orders[lifecycle.tid] = lifecycle
        self._persist_lifecycle(lifecycle)

    def unregister(self, tid: str) -> None:
        """Убирает ордер из реестра."""
        lifecycle = None
        with self._lock:
            lifecycle = self._orders.pop(tid, None)
        if lifecycle:
            delete_pending_order(lifecycle.strategy_id, tid)

    def get_pending(self) -> list[OrderLifecycle]:
        """Возвращает ордера, которые нужно проверить на late fills."""
        with self._lock:
            return list(self._orders.values())

    def recover_strategy_orders(self, connector, strategy_id: str) -> dict:
        """Восстанавливает pending-ордера стратегии по данным брокера."""
        recovered = []
        unresolved = []

        with self._lock:
            pending = [
                lifecycle
                for lifecycle in self._orders.values()
                if lifecycle.strategy_id == strategy_id
            ]

        for lifecycle in pending:
            try:
                info = connector.get_order_status(lifecycle.tid)
            except Exception as exc:
                unresolved.append({
                    "tid": lifecycle.tid,
                    "reason": f"status_error:{exc}",
                    "lifecycle": lifecycle.snapshot(),
                })
                continue

            if not info:
                unresolved.append({
                    "tid": lifecycle.tid,
                    "reason": "missing_on_broker",
                    "lifecycle": lifecycle.snapshot(),
                })
                continue

            balance = info.get("balance")
            quantity = info.get("quantity")
            if balance is None or quantity is None:
                unresolved.append({
                    "tid": lifecycle.tid,
                    "reason": "incomplete_status_payload",
                    "lifecycle": lifecycle.snapshot(),
                })
                continue

            filled = int(quantity) - int(balance)
            avg_price = info.get("avg_price") or info.get("price") or 0.0
            try:
                avg_price = float(avg_price)
            except (TypeError, ValueError):
                avg_price = 0.0

            event = lifecycle.update_from_connector(
                str(info.get("status", "") or ""),
                filled,
                avg_price,
            )
            self._persist_lifecycle(lifecycle)
            recovered.append({
                "tid": lifecycle.tid,
                "status": lifecycle.state.value,
                "event": event,
                "filled_qty": lifecycle.filled_qty,
                "avg_price": lifecycle.avg_price,
            })

        return {
            "recovered": recovered,
            "unresolved": unresolved,
        }

    def check_late_fills(self, connector) -> list[dict]:
        """Проверяет все pending ордера на late fills.

        Вызывается периодически (из Reconciler или dedicated thread).

        Returns:
            Список словарей с обнаруженными late fills:
            [{"tid": ..., "strategy_id": ..., "delta": ..., "lifecycle": ...}, ...]
        """
        results = []
        to_remove = []

        with self._lock:
            pending = list(self._orders.items())

        for tid, lifecycle in pending:
            try:
                info = connector.get_order_status(tid)
                if not info:
                    continue

                status = info.get("status", "")
                balance = info.get("balance")
                quantity = info.get("quantity")
                avg_price = info.get("avg_price") or info.get("price") or 0.0

                if balance is not None and quantity is not None:
                    filled = int(quantity) - int(balance)
                else:
                    continue

                try:
                    avg_price = float(avg_price)
                except (TypeError, ValueError):
                    avg_price = 0.0

                event = lifecycle.update_from_connector(status, filled, avg_price)

                if event == "late_fill":
                    self._persist_lifecycle(lifecycle)
                    delta = lifecycle.get_late_fill_delta()
                    results.append({
                        "tid": tid,
                        "strategy_id": lifecycle.strategy_id,
                        "ticker": lifecycle.ticker,
                        "side": lifecycle.side,
                        "delta": delta,
                        "total_filled": lifecycle.filled_qty,
                        "avg_price": lifecycle.avg_price,
                        "lifecycle": lifecycle,
                    })
                    logger.warning(
                        f"[PendingOrderRegistry] Late fill detected: "
                        f"tid={tid} [{lifecycle.strategy_id}] +{delta} fills"
                    )

            except Exception as e:
                logger.debug(
                    f"[PendingOrderRegistry] Ошибка проверки tid={tid}: {e}"
                )

        # Удаляем старые записи
        now = time.time()
        with self._lock:
            for tid, lc in list(self._orders.items()):
                if lc._terminal_at_epoch and (now - lc._terminal_at_epoch) > self._max_age_sec:
                    to_remove.append(tid)
            for tid in to_remove:
                lifecycle = self._orders.pop(tid, None)
                if lifecycle:
                    delete_pending_order(lifecycle.strategy_id, tid)

        return results

    def cleanup_expired(self) -> int:
        """Удаляет устаревшие записи. Возвращает количество удалённых."""
        now = time.time()
        removed = 0
        with self._lock:
            for tid in list(self._orders):
                lc = self._orders[tid]
                if lc._terminal_at_epoch and (now - lc._terminal_at_epoch) > self._max_age_sec:
                    del self._orders[tid]
                    delete_pending_order(lc.strategy_id, tid)
                    removed += 1
        return removed


# Module-level singleton
pending_order_registry = PendingOrderRegistry()
