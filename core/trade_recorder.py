# core/trade_recorder.py

"""
Trade Recorder — запись сделок в историю.

Инкапсулирует:
- Запись через canonical FillLedger (order_history + trades_history)
- Расчёт комиссии
- Загрузка point_cost
- Обновление equity tracker
"""

from datetime import datetime
from typing import Callable, Optional

from loguru import logger

from core.fill_ledger import fill_ledger
from core.order_history import get_total_pnl
from core.equity_tracker import record_equity
from core.valuation_service import valuation_service


class TradeRecorder:
    """Регистратор сделок для одной стратегии.

    Зависимости:
        order_history: функции make_order, save_order
        append_trade: функция из storage
        commission_manager: расчёт комиссии
        connector: для получения point_cost
    """

    def __init__(
        self,
        strategy_id: str,
        ticker: str,
        board: str,
        agent_name: str,
        get_point_cost: Callable = None,
        get_lot_size: Callable = None,
        is_futures: Callable = None,
        calculate_commission: Callable = None,
        get_last_price: Callable = None,
        get_position_qty: Callable = None,
        get_entry_price: Callable = None,
    ):
        self._strategy_id = strategy_id
        self._ticker = ticker
        self._board = board
        self._agent_name = agent_name
        self._get_point_cost = get_point_cost or (lambda: 1.0)
        self._get_lot_size = get_lot_size or (lambda: 1)
        self._is_futures = is_futures or (lambda: False)
        self._calculate_commission = calculate_commission
        self._get_last_price = get_last_price or (lambda: 0.0)
        self._get_position_qty = get_position_qty or (lambda: 0)
        self._get_entry_price = get_entry_price or (lambda: 0.0)

    def record_trade(
        self,
        side: str,
        qty: int,
        price: float,
        comment: str,
        order_type: str = "market",
        order_ref: str = "",
        correlation_id: str = "",
    ):
        """Записывает исполненную сделку в order_history и trades_history.

        Args:
            side: 'buy' или 'sell'
            qty: Количество
            price: Цена исполнения
            comment: Комментарий
            order_type: Тип ордера (market, limit, chase)
            order_ref: Ссылка на ордер (execution_id)
        """
        # Рассчитываем комиссию
        commission_rub = 0.0
        commission_per_lot = 0.0
        if self._calculate_commission:
            commission_rub = self._calculate_commission(self._ticker, qty, price)
            commission_per_lot = commission_rub / abs(qty) if qty != 0 else 0

        logger.info(
            f"[TradeRecorder:{self._strategy_id}] Запись сделки: {side.upper()} {self._ticker} "
            f"x{qty} @ {price:.4f}, комиссия={commission_rub:.2f} руб "
            f"({commission_per_lot:.2f} руб/лот)"
        )

        try:
            # execution_id из коннектора — единый источник правды
            exec_id = order_ref or ""
            if not exec_id:
                logger.warning(
                    f"[TradeRecorder:{self._strategy_id}] Пропускаем запись сделки без execution_id: "
                    f"{side.upper()} {self._ticker} x{qty} @ {price} ({order_type})"
                )
                return

            # pnl_multiplier: делегируем в ValuationService
            pnl_mult = valuation_service.get_pnl_multiplier(
                is_futures=self._is_futures(),
                point_cost=self._get_point_cost(),
                lot_size=self._get_lot_size(),
            )

            # Canonical fill — единая точка записи
            result = fill_ledger.record_fill(
                fill_id=exec_id,
                strategy_id=self._strategy_id,
                ticker=self._ticker,
                board=self._board,
                side=side,
                qty=qty,
                price=price,
                agent_name=self._agent_name,
                comment=comment,
                order_type=order_type,
                commission_per_lot=commission_per_lot,
                commission_total=commission_rub,
                point_cost=self._get_point_cost(),
                pnl_multiplier=pnl_mult,
                source="connector",
                correlation_id=correlation_id or exec_id,
            )
            if result.error:
                raise RuntimeError(
                    f"fill projection failed: {result.error} "
                    f"(order={result.order_status}, trade={result.trade_status})"
                )

        except Exception as e:
            logger.error(
                f"[TradeRecorder:{self._strategy_id}] Ошибка записи сделки "
                f"(данные могут быть частично записаны): {side.upper()} {self._ticker} "
                f"x{qty} @ {price} exec_id={order_ref} | {e}"
            )

        # Принудительный flush equity после каждой сделки
        self._flush_equity()

    def _flush_equity(self):
        """Записывает текущий equity для трекинга просадки."""
        try:
            realized = get_total_pnl(self._strategy_id) or 0.0
            position_qty = self._get_position_qty()
            last_price = self._get_last_price()
            entry_price = self._get_entry_price()

            pnl_multiplier = valuation_service.get_pnl_multiplier(
                is_futures=self._is_futures(),
                point_cost=self._get_point_cost(),
                lot_size=self._get_lot_size(),
            )

            entry_commission = 0.0
            exit_commission = 0.0
            if position_qty and last_price and entry_price:
                if self._calculate_commission:
                    entry_commission = self._calculate_commission(
                        self._ticker, position_qty, entry_price
                    )
                    exit_commission = self._calculate_commission(
                        self._ticker, position_qty, last_price
                    )

            equity = valuation_service.compute_equity_snapshot(
                realized_pnl=realized,
                entry_price=entry_price or 0.0,
                current_price=last_price or 0.0,
                position_qty=position_qty or 0,
                pnl_multiplier=pnl_multiplier,
                entry_commission=entry_commission,
                exit_commission=exit_commission,
            )

            record_equity(
                self._strategy_id, equity, position_qty or 0, force_flush=True
            )
        except Exception as e:
            logger.warning(f"[TradeRecorder:{self._strategy_id}] equity flush error: {e}")

    def calculate_commission(self, ticker: str, qty: int, price: float) -> float:
        """Публичный метод расчёта комиссии."""
        if self._calculate_commission:
            return self._calculate_commission(ticker, qty, price)
        return 0.0

    def get_point_cost(self) -> float:
        """Возвращает стоимость пункта."""
        return self._get_point_cost()
