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
from datetime import datetime
from typing import Optional

from loguru import logger

from core.order_history import make_order, save_order
from core.storage import append_trade


class FillLedger:
    """Canonical fill ledger — единая точка записи исполнений.

    Дедупликация в памяти по fill_id. Каждый fill проецируется
    в order_history (save_order) и trades_history (append_trade).
    """

    _MAX_SEEN = 5_000

    def __init__(self):
        self._seen_fills: dict[str, float] = {}
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
    ) -> bool:
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
            True — fill записан. False — дубликат или невалидный fill_id.
        """
        if not fill_id:
            logger.warning(
                f"[FillLedger] Пропуск fill без fill_id: "
                f"{side} {ticker} x{qty} [{strategy_id}]"
            )
            return False

        with self._lock:
            if fill_id in self._seen_fills:
                logger.debug(f"[FillLedger] Дубликат fill_id={fill_id}, пропуск")
                return False
            self._seen_fills[fill_id] = time.time()
            if len(self._seen_fills) > self._MAX_SEEN:
                self._cleanup_old_unsafe()

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
        }

        # Атомарная запись: save_order → append_trade
        save_order(order)
        append_trade(trade)

        logger.info(
            f"[FillLedger] Fill записан: {side.upper()} {ticker} x{qty} @ {price:.4f} "
            f"fill_id={fill_id} [{strategy_id}]"
        )
        return True

    def is_duplicate(self, fill_id: str) -> bool:
        """Проверяет, был ли fill с таким ID уже записан."""
        with self._lock:
            return fill_id in self._seen_fills

    def _cleanup_old_unsafe(self):
        """Удаляет старые записи. Вызывать только внутри self._lock."""
        cutoff = time.time() - 86400  # 24 часа
        to_remove = [k for k, v in self._seen_fills.items() if v < cutoff]
        for k in to_remove:
            del self._seen_fills[k]
        # Если всё ещё слишком много — удаляем старейшую половину
        if len(self._seen_fills) > self._MAX_SEEN // 2:
            sorted_items = sorted(self._seen_fills.items(), key=lambda x: x[1])
            for k, _ in sorted_items[: len(sorted_items) // 2]:
                del self._seen_fills[k]


# Module-level singleton
fill_ledger = FillLedger()
