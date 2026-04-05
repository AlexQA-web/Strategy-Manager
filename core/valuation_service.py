# core/valuation_service.py

"""
Единый сервис расчёта стоимости позиции, PnL и комиссий.

Консолидирует все денежные формулы проекта в одном месте:
- PnL multiplier resolution (point_cost / lot_size / bond formula)
- Unrealized (open) PnL
- Realized (closed) PnL
- Commission calculation (делегирует CommissionManager)
- Equity snapshot assembly

Никакой модуль за пределами ValuationService не должен содержать
локальных денежных формул. Вместо этого вызывается один из публичных
методов данного сервиса.
"""

from typing import Optional

from loguru import logger


class ValuationService:
    """Центральный расчётчик PnL, комиссий и equity."""

    # ------------------------------------------------------------------
    # PnL Multiplier
    # ------------------------------------------------------------------

    @staticmethod
    def get_pnl_multiplier(
        *,
        is_futures: bool,
        point_cost: float = 0.0,
        lot_size: int = 1,
    ) -> float:
        """Возвращает денежный множитель для расчёта PnL.

        Для фьючерсов: point_cost (стоимость пункта цены).
        Для акций/ETF:  lot_size  (количество бумаг в лоте).
        Для облигаций:  lot_size  (обычно 1).
        """
        if is_futures:
            return point_cost if point_cost > 0 else 1.0
        return float(lot_size) if lot_size > 0 else 1.0

    # ------------------------------------------------------------------
    # Unrealized (open) PnL
    # ------------------------------------------------------------------

    @staticmethod
    def compute_open_pnl(
        *,
        entry_price: float,
        current_price: float,
        qty: int,
        pnl_multiplier: float,
        entry_commission: float = 0.0,
        exit_commission: float = 0.0,
    ) -> float:
        """Рассчитывает net unrealized PnL по открытой позиции.

        Args:
            entry_price: Средняя цена входа.
            current_price: Текущая рыночная цена.
            qty: Количество (положительное = long, отрицательное = short).
            pnl_multiplier: Денежный множитель (point_cost или lot_size).
            entry_commission: Комиссия за вход (абс. руб.).
            exit_commission: Комиссия за предполагаемый выход (абс. руб.).

        Returns:
            Net unrealized PnL в рублях.
        """
        gross = (current_price - entry_price) * qty * pnl_multiplier
        return gross - entry_commission - exit_commission

    # ------------------------------------------------------------------
    # Realized (closed) PnL — для одной FIFO-пары
    # ------------------------------------------------------------------

    @staticmethod
    def compute_closed_pnl(
        *,
        open_price: float,
        close_price: float,
        qty: int,
        is_long: bool,
        pnl_multiplier: float,
        entry_commission: float = 0.0,
        exit_commission: float = 0.0,
    ) -> float:
        """Рассчитывает net realized PnL для одной FIFO-пары.

        Args:
            open_price: Цена открытия.
            close_price: Цена закрытия.
            qty: Количество в паре (абсолютное).
            is_long: True — пара long (buy→sell), False — short (sell→buy).
            pnl_multiplier: Денежный множитель.
            entry_commission: Абсолютная комиссия за вход.
            exit_commission: Абсолютная комиссия за выход.

        Returns:
            Net realized PnL в рублях.
        """
        if is_long:
            gross = (close_price - open_price) * qty * pnl_multiplier
        else:
            gross = (open_price - close_price) * qty * pnl_multiplier
        return gross - entry_commission - exit_commission

    # ------------------------------------------------------------------
    # Commission
    # ------------------------------------------------------------------

    @staticmethod
    def compute_commission(
        *,
        ticker: str,
        board: str,
        qty: int,
        price: float,
        commission_manager,
        point_cost: float = 0.0,
        lot_size: int = 1,
        connector_id: str = "transaq",
        order_role: str = "taker",
    ) -> float:
        """Рассчитывает комиссию для одной стороны сделки.

        Делегирует расчёт в CommissionManager. Если CommissionManager
        недоступен, возвращает 0.0.

        Args:
            ticker: Тикер.
            board: Борда.
            qty: Количество (лоты/контракты).
            price: Цена.
            commission_manager: Экземпляр CommissionManager.
            point_cost: Стоимость пункта (для фьючерсов).
            lot_size: Размер лота (для акций/ETF).
            connector_id: ID коннектора.
            order_role: "taker" или "maker".

        Returns:
            Комиссия в рублях.
        """
        if commission_manager is None:
            return 0.0
        return commission_manager.calculate(
            ticker=ticker,
            board=board,
            quantity=abs(qty),
            price=price,
            order_role=order_role,
            point_cost=point_cost,
            connector_id=connector_id,
            lot_size=lot_size,
        )

    # ------------------------------------------------------------------
    # Equity snapshot
    # ------------------------------------------------------------------

    @staticmethod
    def compute_equity_snapshot(
        *,
        realized_pnl: float,
        entry_price: float,
        current_price: float,
        position_qty: int,
        pnl_multiplier: float,
        entry_commission: float = 0.0,
        exit_commission: float = 0.0,
    ) -> float:
        """Рассчитывает equity = realized + unrealized PnL.

        Args:
            realized_pnl: Кумулятивный realized PnL (из order_history).
            entry_price: Средняя цена входа текущей позиции.
            current_price: Текущая цена.
            position_qty: Количество в текущей позиции (±).
            pnl_multiplier: Денежный множитель.
            entry_commission: Абс. комиссия за вход.
            exit_commission: Абс. комиссия за выход.

        Returns:
            Общий equity в рублях.
        """
        if position_qty and current_price and entry_price:
            unrealized = ValuationService.compute_open_pnl(
                entry_price=entry_price,
                current_price=current_price,
                qty=position_qty,
                pnl_multiplier=pnl_multiplier,
                entry_commission=entry_commission,
                exit_commission=exit_commission,
            )
        else:
            unrealized = 0.0
        return realized_pnl + unrealized

    # ------------------------------------------------------------------
    # Commission slicing (для FIFO partial matching)
    # ------------------------------------------------------------------

    @staticmethod
    def slice_commission(
        total_commission: float,
        slice_qty: int,
        source_qty: int,
    ) -> float:
        """Пропорционально делит комиссию при частичном матчинге FIFO."""
        if total_commission <= 0 or slice_qty <= 0 or source_qty <= 0:
            return 0.0
        return total_commission * (slice_qty / source_qty)


# Module-level singleton
valuation_service = ValuationService()
