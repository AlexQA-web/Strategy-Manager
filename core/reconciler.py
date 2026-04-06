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
from dataclasses import dataclass
from typing import Callable, Optional

from loguru import logger

from core.telegram_bot import notifier, EventCode
from core.order_lifecycle import pending_order_registry
from core.runtime_metrics import runtime_metrics


@dataclass
class ReconcileResult:
    status: str
    mismatch: bool = False
    broker_qty: Optional[int] = None
    history_qty: Optional[int] = None
    internal_qty: Optional[int] = None
    history_error: Optional[str] = None
    aggregate_history_qty: Optional[int] = None


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
        on_history_divergence: Callable[[str], None] = None,
        allow_shared_position: bool = False,
    ):
        self._strategy_id = strategy_id
        self._ticker = ticker
        self._account_id = account_id
        self._connector = connector
        self._position_tracker = position_tracker
        self._get_order_pairs = get_order_pairs
        self._detect_position = detect_position
        self._on_broker_unavailable = on_broker_unavailable
        self._on_history_divergence = on_history_divergence
        self._allow_shared_position = allow_shared_position

        self._reconcile_interval_sec = reconcile_interval_sec
        self._alert_cooldown_sec = alert_cooldown_sec
        self._last_reconcile_ts = 0.0
        self._last_reconcile_alert_ts = 0.0
        self._mismatch_streak = 0
        self._self_heal_threshold = 3
        self._self_heal_cooldown_sec = 300.0
        self._last_self_heal_ts = 0.0

    def reconcile(self) -> bool:
        """Периодическая сверка: connector <-> engine <-> order_history.

        Returns:
            True если была рассинхронизация, False если всё в порядке.
        """
        return self.reconcile_result().mismatch

    def reconcile_result(self) -> ReconcileResult:
        """Явный результат reconcile без bool-ambiguity."""
        now = time.monotonic()
        if now - self._last_reconcile_ts < self._reconcile_interval_sec:
            return ReconcileResult(status="skipped_interval")
        self._last_reconcile_ts = now

        # Не сверкаем если ордер в полёте
        if self._position_tracker.is_order_in_flight():
            return ReconcileResult(status="skipped_in_flight")

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
            return ReconcileResult(
                status="broker_unavailable",
                broker_qty=None,
                internal_qty=internal_qty,
            )

        # Получаем qty из истории
        history_qty, history_error = self._get_history_qty_result()
        if history_error is not None:
            logger.warning(
                f"[Reconciler:{self._strategy_id}] History unavailable for {self._ticker}, "
                f"skipping reconcile. error={history_error}"
            )
            runtime_metrics.emit_audit_event(
                "history_unavailable",
                strategy_id=self._strategy_id,
                ticker=self._ticker,
                broker_qty=broker_qty,
                internal_qty=internal_qty,
                error=history_error,
            )
            return ReconcileResult(
                status="history_unavailable",
                broker_qty=broker_qty,
                history_qty=None,
                internal_qty=internal_qty,
                history_error=history_error,
            )

        aggregate_history_qty, aggregate_history_error, aggregate_strategy_count = (
            self._get_account_aggregate_history_qty_result()
        )
        aggregate_mode = self._allow_shared_position and aggregate_strategy_count > 1
        if aggregate_history_error is not None:
            logger.warning(
                f"[Reconciler:{self._strategy_id}] Aggregate history unavailable for {self._ticker}, "
                f"error={aggregate_history_error}"
            )

        mismatch = False
        self_heal_requested = False

        # Сверка engine vs broker
        if not aggregate_mode and broker_qty != internal_qty:
            msg = (
                f"Reconcile mismatch engine vs broker: ticker={self._ticker} "
                f"engine_qty={internal_qty} broker_qty={broker_qty}."
            )
            logger.warning(f"[Reconciler:{self._strategy_id}] {msg}")
            self._send_alert(msg)
            self_heal_requested = True
            mismatch = True

        # Сверка history vs broker
        if not aggregate_mode and broker_qty != history_qty:
            msg = (
                f"Reconcile mismatch history vs broker: ticker={self._ticker} "
                f"history_qty={history_qty} broker_qty={broker_qty}."
            )
            logger.warning(f"[Reconciler:{self._strategy_id}] {msg}")
            self._send_alert(msg)
            if self._on_history_divergence:
                runtime_metrics.emit_audit_event(
                    "history_divergence",
                    strategy_id=self._strategy_id,
                    ticker=self._ticker,
                    broker_qty=broker_qty,
                    history_qty=history_qty,
                    internal_qty=internal_qty,
                )
                try:
                    self._on_history_divergence(msg)
                except Exception as exc:
                    logger.warning(
                        f"[Reconciler:{self._strategy_id}] history divergence callback error: {exc}"
                    )
            else:
                self_heal_requested = True
            mismatch = True

        if aggregate_mode and aggregate_history_qty is not None and broker_qty != aggregate_history_qty:
            msg = (
                f"Reconcile mismatch account aggregate vs broker: account={self._account_id} "
                f"ticker={self._ticker} aggregate_qty={aggregate_history_qty} broker_qty={broker_qty}."
            )
            logger.warning(f"[Reconciler:{self._strategy_id}] {msg}")
            self._send_alert(msg)
            runtime_metrics.emit_audit_event(
                "account_aggregate_divergence",
                strategy_id=self._strategy_id,
                account_id=self._account_id,
                ticker=self._ticker,
                broker_qty=broker_qty,
                aggregate_history_qty=aggregate_history_qty,
                strategy_count=aggregate_strategy_count,
            )
            if self._on_history_divergence:
                try:
                    self._on_history_divergence(msg)
                except Exception as exc:
                    logger.warning(
                        f"[Reconciler:{self._strategy_id}] aggregate divergence callback error: {exc}"
                    )
            mismatch = True

        if mismatch:
            self._mismatch_streak += 1
            if self_heal_requested:
                self._maybe_run_self_heal()
        else:
            self._mismatch_streak = 0

        return ReconcileResult(
            status="mismatch" if mismatch else "ok",
            mismatch=mismatch,
            broker_qty=broker_qty,
            history_qty=history_qty,
            internal_qty=internal_qty,
            aggregate_history_qty=aggregate_history_qty,
        )

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

    def _get_history_qty_result(self) -> tuple[Optional[int], Optional[str]]:
        """Получает qty из order_history.

        Returns:
            (qty, None) при успешном чтении.
            (None, error) если history недоступна.
        """
        if not self._get_order_pairs:
            return 0, None
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
            return int(total), None
        except Exception as exc:
            return None, str(exc)

    def _get_history_qty(self) -> int:
        """Совместимый wrapper поверх history qty result API."""
        qty, _ = self._get_history_qty_result()
        return int(qty or 0)

    def _get_account_aggregate_history_qty_result(self) -> tuple[Optional[int], Optional[str], int]:
        if not self._allow_shared_position:
            return self._get_history_qty(), None, 1

        try:
            from core.storage import get_all_strategies
            from core.strategy_position_book import get_strategy_position_book

            strategies = get_all_strategies()
            total_qty = 0
            strategy_count = 0
            ticker_value = str(self._ticker or "").upper()
            board_value = str(getattr(self, "_board", "") or "").upper()

            for strategy_id, data in strategies.items():
                if not isinstance(data, dict):
                    continue
                strategy_account = str(data.get("account_id") or data.get("finam_account") or "")
                strategy_ticker = str(data.get("ticker") or data.get("params", {}).get("ticker", "") or "").upper()
                strategy_board = str(data.get("board", "") or "").upper()

                if strategy_account != self._account_id or strategy_ticker != ticker_value:
                    continue
                if board_value and strategy_board and strategy_board != board_value:
                    continue

                strategy_count += 1
                for entry in get_strategy_position_book(strategy_id, ticker=ticker_value, board=board_value or None):
                    qty = int(entry.get("quantity", 0) or 0)
                    side = str(entry.get("side", "")).lower()
                    total_qty += qty if side == "buy" else -qty

            return total_qty, None, strategy_count
        except Exception as exc:
            return None, str(exc), 0

    def _run_self_heal(self):
        """Запускает self-heal: синхронизирует engine с broker."""
        if self._detect_position:
            try:
                self._detect_position()
            except Exception as e:
                logger.warning(f"[Reconciler:{self._strategy_id}] self-heal error: {e}")

    def _maybe_run_self_heal(self):
        now = time.monotonic()
        if self._mismatch_streak < self._self_heal_threshold:
            logger.info(
                f"[Reconciler:{self._strategy_id}] self-heal deferred: "
                f"streak={self._mismatch_streak}/{self._self_heal_threshold}"
            )
            return
        if now - self._last_self_heal_ts < self._self_heal_cooldown_sec:
            logger.info(
                f"[Reconciler:{self._strategy_id}] self-heal cooldown active: "
                f"{now - self._last_self_heal_ts:.1f}s/{self._self_heal_cooldown_sec:.1f}s"
            )
            return
        self._last_self_heal_ts = now
        self._run_self_heal()

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
