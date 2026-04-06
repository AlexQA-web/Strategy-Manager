# core/position_tracker.py

"""
Отслеживание торговой позиции.

Инкапсулирует состояние позиции (направление, количество, цена входа)
и флаг активного ордера. Все операции потокобезопасны через _position_lock.

Матрица разрешённых переходов (trade path):
    flat  → long     (open_position / confirm_open, side=buy)
    flat  → short    (open_position / confirm_open, side=sell)
    long  → flat     (close_position, полное закрытие)
    short → flat     (close_position, полное закрытие)
    long  → long*    (close_position, частичное закрытие — уменьшение qty)
    short → short*   (close_position, частичное закрытие — уменьшение qty)

Запрещённые переходы (trade path):
    long  → short    (flip — требуется явное закрытие, затем открытие)
    short → long     (flip — требуется явное закрытие, затем открытие)
    long  → long+    (scale-in — увеличение позиции не поддерживается)
    short → short+   (scale-in — увеличение позиции не поддерживается)

Sync path (update_position) — не ограничен, используется для синхронизации
с внешним состоянием брокера.
"""

import threading
from typing import Optional


class PositionTracker:
    """Потокобезопасный трекер позиции для одной стратегии/тикета.

    Атрибуты:
        position: Направление позиции (1=long, -1=short, 0=нет позиции)
        position_qty: Количество контрактов (со знаком)
        entry_price: Цена входа
        order_in_flight: Флаг активного ордера (защита от двойного входа)
    """

    def __init__(self):
        self._position_lock = threading.Lock()
        self._position: int = 0
        self._position_qty: int = 0
        self._entry_price: float = 0.0
        self._order_in_flight: bool = False

    # --- Чтение состояния ---

    def is_in_position(self) -> bool:
        """Возвращает True, если есть открытая позиция."""
        with self._position_lock:
            return self._position != 0

    def get_position(self) -> int:
        """Возвращает направление позиции (1, -1, 0)."""
        with self._position_lock:
            return self._position

    def get_position_qty(self) -> int:
        """Возвращает количество контрактов (со знаком)."""
        with self._position_lock:
            return self._position_qty

    def get_entry_price(self) -> float:
        """Возвращает цену входа."""
        with self._position_lock:
            return self._entry_price

    def is_order_in_flight(self) -> bool:
        """Возвращает True, если ордер в процессе исполнения."""
        with self._position_lock:
            return self._order_in_flight

    def get_state(self) -> dict:
        """Возвращает полное состояние позиции (snapshot)."""
        with self._position_lock:
            return {
                "position": self._position,
                "position_qty": self._position_qty,
                "entry_price": self._entry_price,
                "order_in_flight": self._order_in_flight,
            }

    # --- Изменение состояния ---

    def open_position(self, side: str, qty: int, price: float) -> bool:
        """Открыть новую позицию.

        Args:
            side: 'buy' или 'sell'
            qty: Количество контрактов (положительное)
            price: Цена исполнения

        Returns:
            True если позиция успешно открыта, False если уже есть позиция или ордер в полёте.
        """
        with self._position_lock:
            if self._position != 0:
                return False
            if self._order_in_flight:
                return False

            self._order_in_flight = True
            self._position = 1 if side == "buy" else -1
            self._position_qty = qty if side == "buy" else -qty
            self._entry_price = price
            return True

    def close_position(self, filled: int, total_qty: int) -> bool:
        """Закрыть позицию (полностью или частично).

        Args:
            filled: Фактически исполненное количество
            total_qty: Запрошенное количество для закрытия

        Returns:
            True если позиция была открыта, False если нет.
        """
        with self._position_lock:
            if self._position == 0:
                return False

            if filled >= total_qty:
                # Полное закрытие
                self._position = 0
                self._position_qty = 0
                self._entry_price = 0.0
            else:
                # Частичное закрытие
                remaining = abs(self._position_qty) - filled
                self._position_qty = remaining if self._position == 1 else -remaining
                if remaining == 0:
                    self._position = 0
                    self._entry_price = 0.0
            return True

    def sync_position(self, position: int, qty: int, entry_price: float):
        """Authoritative sync-path update from broker/reconcile.

        Не ограничен матрицей переходов, т.к. отражает внешнее состояние.
        Одновременно сбрасывает transient order_in_flight, чтобы reconcile не
        оставлял стратегию заблокированной после подтверждённого broker sync.
        """
        with self._position_lock:
            self._position = position
            self._position_qty = qty
            self._entry_price = entry_price
            self._order_in_flight = False

    def update_position(self, position: int, qty: int, entry_price: float):
        """Совместимый алиас sync-path обновления позиции."""
        self.sync_position(position, qty, entry_price)

    def confirm_open(self, side: str, filled: int, price: float) -> bool:
        """Подтвердить открытие позиции после исполнения ордера (trade path).

        Проверяет матрицу переходов: позиция должна быть flat.
        Запрещает flip и scale-in.

        Args:
            side: 'buy' или 'sell'
            filled: Исполненное количество (положительное)
            price: Цена исполнения

        Returns:
            True если позиция успешно установлена.
            False если переход запрещён (flip или scale-in).
        """
        with self._position_lock:
            new_direction = 1 if side == "buy" else -1

            if self._position != 0:
                if self._position != new_direction:
                    # Flip: long→short или short→long
                    from loguru import logger
                    logger.error(
                        f"PositionTracker.confirm_open: запрещён flip "
                        f"{self._position}→{new_direction} (side={side}, filled={filled})"
                    )
                    return False
                else:
                    # Scale-in: увеличение позиции
                    from loguru import logger
                    logger.error(
                        f"PositionTracker.confirm_open: запрещён scale-in "
                        f"(pos={self._position}, qty={self._position_qty}, "
                        f"side={side}, filled={filled})"
                    )
                    return False

            self._position = new_direction
            self._position_qty = filled if side == "buy" else -filled
            self._entry_price = price
            return True

    def try_set_order_in_flight(self) -> bool:
        """Атомарно проверить отсутствие позиции и ордера, и установить флаг in-flight.

        Устраняет TOCTOU race condition: check-and-set в одном lock acquisition.

        Returns:
            True если флаг успешно установлен (не было позиции и ордера в полёте).
            False если позиция уже открыта или ордер уже в работе.
        """
        with self._position_lock:
            if self._position != 0 or self._order_in_flight:
                return False
            self._order_in_flight = True
            return True

    def try_set_order_in_flight_for_close(self) -> bool:
        """Атомарно проверить наличие позиции и отсутствие ордера, и установить флаг in-flight.

        Для операций закрытия: требуется наличие позиции и отсутствие ордера в полёте.

        Returns:
            True если флаг успешно установлен.
            False если нет позиции или ордер уже в работе.
        """
        with self._position_lock:
            if self._position == 0 or self._order_in_flight:
                return False
            self._order_in_flight = True
            return True

    def set_order_in_flight(self, value: bool):
        """Установить флаг активного ордера."""
        with self._position_lock:
            self._order_in_flight = value

    def clear_order_in_flight(self):
        """Сбросить флаг активного ордера."""
        with self._position_lock:
            self._order_in_flight = False

    def reset(self):
        """Полный сброс состояния (для тестов или перезапуска)."""
        with self._position_lock:
            self._position = 0
            self._position_qty = 0
            self._entry_price = 0.0
            self._order_in_flight = False
