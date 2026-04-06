# core/fill_ledger.py

"""
Canonical Fill Ledger — единственный источник истины по исполненным сделкам.

Каждое исполнение (fill) записывается с уникальным fill_id (внешний execution_id
или tradeno коннектора). Дедупликация происходит по fill_id.

order_history и trades_history — производные представления, записываемые
из fill ledger для обратной совместимости.

Потребители:
    - TradeRecorder.record_trade() → делегирует сюда
    - FinamConnector._parse_trades() → делегирует сюда
    - Любой будущий источник fills

Гарантии:
    - Один fill_id записывается ровно один раз
    - При записи fill проецируется в order_history И trades_history атомарно
    - Потокобезопасность через threading.Lock
"""

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from loguru import logger

from core.order_history import make_order, save_order
from core.runtime_metrics import runtime_metrics
from core.storage import append_trade
from core.telegram_bot import notifier, EventCode


@dataclass
class ProjectionResult:
    fill_id: str
    order_status: str
    trade_status: str
    error: str = ""

    @property
    def is_duplicate(self) -> bool:
        return self.order_status == "duplicate" and self.trade_status == "duplicate"

    @property
    def is_repair(self) -> bool:
        statuses = {self.order_status, self.trade_status}
        return statuses == {"inserted", "duplicate"} and not self.error

    @property
    def is_success(self) -> bool:
        return not self.error and (self.is_duplicate or self.is_repair or {
            self.order_status,
            self.trade_status,
        } == {"inserted"})


@dataclass
class FillReservation:
    status: str
    updated_at: float


class FillLedger:
    """Canonical fill ledger — единая точка записи исполнений.

    Дедупликация в памяти по fill_id с атомарным резервированием.
    Каждый fill проецируется
    в order_history (save_order) и trades_history (append_trade).
    """

    _MAX_SEEN = 5_000
    _PROCESSING_TTL_SEC = 300

    def __init__(self):
        self._seen_fills: dict[str, FillReservation] = {}
        self._lock = threading.Lock()

    def record_fill(
        self,
        fill_id: str,
        strategy_id: str,
        ticker: str,
        board: str,
        side: str,
        qty: int,
        price: float,
        agent_name: str = "",
        comment: str = "",
        order_type: str = "market",
        commission_per_lot: float = 0.0,
        commission_total: float = 0.0,
        point_cost: float = 1.0,
        pnl_multiplier: float = 0.0,
        source: str = "",
        timestamp: str = "",
        correlation_id: str = "",
    ) -> ProjectionResult:
        """Записывает канонический fill event.

        Args:
            fill_id: Уникальный идентификатор исполнения (exec_id, tradeno, chase ref).
            strategy_id: ID стратегии (agent).
            ticker: Тикер инструмента.
            board: Режим торгов (TQBR, SPBFUT, ...).
            side: 'buy' или 'sell'.
            qty: Количество исполненных лотов.
            price: Цена исполнения.
            agent_name: Имя агента (для trades_history).
            comment: Комментарий.
            order_type: Тип ордера (market, limit, chase).
            commission_per_lot: Комиссия в руб. за 1 лот.
            commission_total: Абсолютная комиссия за всю сторону в руб.
            point_cost: Стоимость пункта.
            pnl_multiplier: Денежный множитель для PnL.
            source: Источник fill (connector, callback, ...).
            timestamp: ISO-время fill. Если пусто — datetime.now().

        Returns:
            ProjectionResult с итогом проекции.
        """
        if not fill_id:
            logger.warning(
                f"[FillLedger] Пропуск fill без fill_id: "
                f"{side} {ticker} x{qty} [{strategy_id}]"
            )
            return ProjectionResult(fill_id="", order_status="error", trade_status="error", error="missing_fill_id")

        if not self._reserve_fill(fill_id):
            logger.debug(f"[FillLedger] Дубликат fill_id={fill_id}, пропуск")
            return ProjectionResult(fill_id=fill_id, order_status="duplicate", trade_status="duplicate")

        # --- Проекция в order_history ---
        ts = timestamp or datetime.now().isoformat()

        order = make_order(
            strategy_id=strategy_id,
            ticker=ticker,
            side=side,
            quantity=qty,
            price=price,
            board=board,
            comment=comment,
            commission=commission_per_lot,
            commission_total=commission_total,
            point_cost=point_cost,
            pnl_multiplier=pnl_multiplier,
            exec_key=fill_id,
            source=source,
            correlation_id=correlation_id or fill_id,
        )
        if timestamp:
            order["timestamp"] = timestamp

        # --- Проекция в trades_history ---
        trade = {
            "strategy_id": strategy_id,
            "agent_name": agent_name,
            "ticker": ticker,
            "board": board,
            "side": side,
            "qty": qty,
            "price": price,
            "commission": commission_total,
            "order_type": order_type,
            "comment": comment,
            "dt": ts,
            "execution_id": fill_id,
            "correlation_id": correlation_id or fill_id,
        }

        order_status = "error"
        trade_status = "error"
        try:
            order_status = save_order(order) or "inserted"
            trade_status = append_trade(trade) or "inserted"
        except Exception as exc:
            self._release_fill(fill_id)
            result = ProjectionResult(
                fill_id=fill_id,
                order_status=order_status,
                trade_status=trade_status,
                error=str(exc),
            )
            logger.error(
                f"[FillLedger] Projection error fill_id={fill_id}: "
                f"order={order_status} trade={trade_status} err={exc}"
            )
            return result

        result = ProjectionResult(
            fill_id=fill_id,
            order_status=order_status,
            trade_status=trade_status,
        )

        if not result.is_success:
            self._release_fill(fill_id)
            result.error = "projection_divergence"
            logger.error(
                f"[FillLedger] Projection divergence fill_id={fill_id}: "
                f"order={order_status} trade={trade_status}"
            )
            return result

        self._commit_fill(fill_id)

        if result.is_duplicate:
            logger.debug(f"[FillLedger] Durable duplicate fill_id={fill_id}, пропуск")
        elif result.is_repair:
            runtime_metrics.emit_audit_event(
                "duplicate_fill_repair",
                strategy_id=strategy_id,
                fill_id=fill_id,
                ticker=ticker,
                side=side,
                qty=qty,
                price=price,
            )
            logger.warning(
                f"[FillLedger] Projection repair fill_id={fill_id}: "
                f"order={order_status} trade={trade_status}"
            )
            try:
                notifier.send(
                    EventCode.STRATEGY_ERROR,
                    agent=strategy_id,
                    description=(
                        f"Duplicate fill repair: {side.upper()} {ticker} x{qty} @ {price:.4f} "
                        f"fill_id={fill_id}"
                    ),
                )
            except Exception:
                pass
        else:
            logger.info(
                f"[FillLedger] Fill записан: {side.upper()} {ticker} x{qty} @ {price:.4f} "
                f"fill_id={fill_id} [{strategy_id}]"
            )
        return result

    def is_duplicate(self, fill_id: str) -> bool:
        """Проверяет, был ли fill с таким ID уже записан."""
        with self._lock:
            return fill_id in self._seen_fills

    def _reserve_fill(self, fill_id: str) -> bool:
        with self._lock:
            if fill_id in self._seen_fills:
                return False
            self._seen_fills[fill_id] = FillReservation(
                status="processing",
                updated_at=time.time(),
            )
            return True

    def _commit_fill(self, fill_id: str):
        with self._lock:
            self._seen_fills[fill_id] = FillReservation(
                status="committed",
                updated_at=time.time(),
            )
            if len(self._seen_fills) > self._MAX_SEEN:
                self._cleanup_old_unsafe()

    def _release_fill(self, fill_id: str):
        with self._lock:
            state = self._seen_fills.get(fill_id)
            if state and state.status == "processing":
                del self._seen_fills[fill_id]

    def _cleanup_old_unsafe(self):
        """Удаляет старые записи. Вызывать только внутри self._lock."""
        now = time.time()
        committed_cutoff = now - 86400  # 24 часа
        processing_cutoff = now - self._PROCESSING_TTL_SEC
        to_remove = [
            fill_id
            for fill_id, reservation in self._seen_fills.items()
            if (
                reservation.status == "committed" and reservation.updated_at < committed_cutoff
            ) or (
                reservation.status == "processing" and reservation.updated_at < processing_cutoff
            )
        ]
        for k in to_remove:
            del self._seen_fills[k]

        # Если всё ещё слишком много — удаляем старейшие committed записи,
        # не вытесняя свежие processing-резервации.
        if len(self._seen_fills) > self._MAX_SEEN // 2:
            committed_items = sorted(
                (
                    (fill_id, reservation)
                    for fill_id, reservation in self._seen_fills.items()
                    if reservation.status == "committed"
                ),
                key=lambda item: item[1].updated_at,
            )
            overflow = len(self._seen_fills) - (self._MAX_SEEN // 2)
            for k, _ in committed_items[:overflow]:
                del self._seen_fills[k]


# Module-level singleton
fill_ledger = FillLedger()
