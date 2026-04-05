# core/reconciler.py

"""
Reconciler — сверка позиции между engine, broker и order_history.

Инкапсулирует:
- Периодическую сверку (reconcile)
- Получение qty из order_history
- Определение позиции из коннектора
- Отправку алертов при рассинхроне
"""

import time
from typing import Callable, Optional

from loguru import logger

from core.telegram_bot import notifier, EventCode
from core.order_lifecycle import pending_order_registry


class Reconciler:
    """Сверка позиции для одной стратегии/тикета.

    Зависимости:
        connector: коннектор к бирже
        position_tracker: трекер позиции
        order_history: функции для получения истории
    """

    def __init__(
        self,
        strategy_id: str,
        ticker: str,
        account_id: str,
        connector,
        position_tracker,
        get_order_pairs: Callable = None,
        detect_position: Callable = None,
        reconcile_interval_sec: float = 60.0,
        alert_cooldown_sec: float = 300.0,
        on_broker_unavailable: Callable = None,
    ):
        self._strategy_id = strategy_id
        self._ticker = ticker
        self._account_id = account_id
        self._connector = connector
        self._position_tracker = position_tracker
        self._get_order_pairs = get_order_pairs
        self._detect_position = detect_position
        self._on_broker_unavailable = on_broker_unavailable

        self._reconcile_interval_sec = reconcile_interval_sec
        self._alert_cooldown_sec = alert_cooldown_sec
        self._last_reconcile_ts = 0.0
        self._last_reconcile_alert_ts = 0.0

    def reconcile(self) -> bool:
        """Периодическая сверка: connector <-> engine <-> order_history.

        Returns:
            True если была рассинхронизация, False если всё в порядке.
        """
        now = time.monotonic()
        if now - self._last_reconcile_ts < self._reconcile_interval_sec:
            return False
        self._last_reconcile_ts = now

        # Не сверкаем если ордер в полёте
        if self._position_tracker.is_order_in_flight():
            return False

        # Проверяем pending ордера на late fills перед сверкой
        self._check_late_fills()

        internal_qty = self._position_tracker.get_position_qty()

        # Получаем qty от брокера
        broker_qty = self._get_broker_qty()

        # Данные брокера недоступны — пропускаем сверку, чтобы не создавать
        # ложный mismatch и не запускать destructive self-heal
        if broker_qty is None:
            logger.info(
                f"[Reconciler:{self._strategy_id}] Broker data unavailable for "
                f"{self._ticker}, skipping reconcile (stale). "
                f"engine_qty={internal_qty}"
            )
            if self._on_broker_unavailable:
                try:
                    self._on_broker_unavailable()
                except Exception:
                    pass
            return False

        # Получаем qty из истории
        history_qty = self._get_history_qty()

        mismatch = False

        # Сверка engine vs broker
        if broker_qty != internal_qty:
            msg = (
                f"Reconcile mismatch engine vs broker: ticker={self._ticker} "
                f"engine_qty={internal_qty} broker_qty={broker_qty}. Running self-heal."
            )
            logger.warning(f"[Reconciler:{self._strategy_id}] {msg}")
            self._send_alert(msg)
            self._run_self_heal()
            mismatch = True

        # Сверка history vs broker
        if broker_qty != history_qty:
            msg = (
                f"Reconcile mismatch history vs broker: ticker={self._ticker} "
                f"history_qty={history_qty} broker_qty={broker_qty}. Running self-heal."
            )
            logger.warning(f"[Reconciler:{self._strategy_id}] {msg}")
            self._send_alert(msg)
            self._run_self_heal()
            mismatch = True

        return mismatch

    def _get_broker_qty(self) -> Optional[int]:
        """Получает qty позиции от брокера.

        Returns:
            int — кол-во лотов (0 если тикер не найден в позициях).
            None — данные брокера недоступны (ошибка запроса).
        """
        try:
            positions = self._connector.get_positions(self._account_id)
            for pos in positions:
                if str(pos.get("ticker", "")) == self._ticker:
                    return int(float(pos.get("quantity", 0) or 0))
        except Exception as e:
            logger.debug(f"[Reconciler:{self._strategy_id}] reconcile get_positions error: {e}")
            return None
        return 0

    def _get_history_qty(self) -> int:
        """Получает qty из order_history (незакрытые позиции)."""
        if not self._get_order_pairs:
            return 0
        try:
            pairs = self._get_order_pairs(self._strategy_id)
            total = 0
            for pair in pairs:
                if pair.get("close") is not None:
                    continue
                open_order = pair.get("open") or {}
                if str(open_order.get("ticker", "")) != self._ticker:
                    continue
                qty = int(open_order.get("quantity", 0) or 0)
                side = str(open_order.get("side", ""))
                total += qty if side == "buy" else -qty
            return int(total)
        except Exception:
            return 0

    def _run_self_heal(self):
        """Запускает self-heal: синхронизирует engine с broker."""
        if self._detect_position:
            try:
                self._detect_position()
            except Exception as e:
                logger.warning(f"[Reconciler:{self._strategy_id}] self-heal error: {e}")

    def _send_alert(self, message: str):
        """Отправляет алерт о рассинхроне с cooldown."""
        now = time.monotonic()
        if now - self._last_reconcile_alert_ts < self._alert_cooldown_sec:
            return
        self._last_reconcile_alert_ts = now
        try:
            notifier.send(
                EventCode.STRATEGY_ERROR,
                agent=self._strategy_id,
                description=message,
            )
        except Exception:
            pass

    def get_history_qty(self) -> int:
        """Публичный метод получения qty из истории."""
        return self._get_history_qty()

    def detect_position(self):
        """Публичный метод определения позиции."""
        if self._detect_position:
            self._detect_position()

    def send_alert(self, message: str):
        """Публичный метод отправки алерта."""
        self._send_alert(message)

    def _check_late_fills(self):
        """Проверяет pending ордера для этой стратегии на late fills."""
        try:
            late_fills = pending_order_registry.check_late_fills(self._connector)
            for lf in late_fills:
                if lf["strategy_id"] == self._strategy_id:
                    delta = lf["delta"]
                    tid = lf["tid"]
                    msg = (
                        f"Late fill detected: tid={tid} {lf['side'].upper()} "
                        f"+{delta} fills @ {lf['avg_price']:.4f}"
                    )
                    logger.warning(f"[Reconciler:{self._strategy_id}] {msg}")
                    self._send_alert(msg)
        except Exception as e:
            logger.debug(f"[Reconciler:{self._strategy_id}] late fill check error: {e}")
