# core/position_manager.py

import threading
from typing import Callable, Optional
from loguru import logger
from core.telegram_bot import notifier, EventCode


class PositionManager:
    """
    Менеджер позиций — слой между коннектором и UI.
    По умолчанию работает с 'finam', переключается через bind().
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._connector_id = "finam"
        self._positions: dict[str, list[dict]] = {}  # account_id → [position, ...]
        self._on_update_callbacks: list[Callable] = []
        self._subscribed = False  # lazy subscription — коннекторы ещё не зарегистрированы при импорте

    # ── Привязка к коннектору ─────────────────────────────────────────────

    def bind(self, connector_id: str):
        """Переключает менеджер на другой коннектор (например, 'quik')."""
        # Отписываемся от старого коннектора перед переключением
        self._unsubscribe()
        self._connector_id = connector_id
        with self._lock:
            self._positions.clear()
        self._subscribed = False  # сброс для нового коннектора
        self._subscribe()
        if self._connector():
            self._subscribed = True
        logger.info(f"PositionManager: переключён на [{connector_id}]")

    def _ensure_subscribed(self):
        """Lazy subscription — вызывается при первом реальном использовании."""
        if not self._subscribed:
            self._subscribe()
            if self._connector():
                self._subscribed = True

    def _unsubscribe(self):
        """Отписывается от старого коннектора перед переключением."""
        old_connector = self._connector()
        if old_connector:
            old_connector.unsubscribe_positions(self._on_positions_update)

    def _subscribe(self):
        connector = self._connector()
        if connector:
            connector.subscribe_positions(self._on_positions_update)

    def _connector(self):
        from core.connector_manager import connector_manager
        return connector_manager.get(self._connector_id)

    # ── Обновление позиций ────────────────────────────────────────────────

    def _on_positions_update(self):
        self._ensure_subscribed()
        connector = self._connector()
        if not connector:
            return
        with self._lock:
            self._positions = dict(connector.get_all_positions())
        self._notify_ui()

    def _notify_ui(self):
        for cb in self._on_update_callbacks:
            try:
                cb()
            except Exception as e:
                logger.warning(f"PositionManager UI callback error: {e}")

    def refresh(self, account_id: str):
        """Принудительный опрос позиций по счёту."""
        self._ensure_subscribed()
        connector = self._connector()
        if not connector or not connector.is_connected():
            logger.warning("PositionManager.refresh: коннектор не подключён")
            return
        raw = connector.get_positions(account_id)
        if raw is not None:
            with self._lock:
                self._positions[account_id] = raw
            self._notify_ui()

    # ── Чтение позиций ────────────────────────────────────────────────────

    def get_positions(self, account_id: str) -> list[dict]:
        with self._lock:
            return list(self._positions.get(account_id, []))

    def get_all_positions(self) -> list[dict]:
        with self._lock:
            return [pos for positions in self._positions.values() for pos in positions]

    def get_position(self, account_id: str, ticker: str) -> Optional[dict]:
        for pos in self.get_positions(account_id):
            if pos.get('ticker') == ticker:
                return pos
        return None

    @staticmethod
    def _format_price(price: Optional[float]) -> str:
        if price is None or price <= 0:
            return 'н/д'
        return f'{price:.4f}'

    def _get_market_price_for_log(self, connector, ticker: str, board: str = 'TQBR') -> str:
        try:
            return self._format_price(connector.get_last_price(ticker, board))
        except Exception:
            return 'н/д'

    # ── Закрытие позиций ──────────────────────────────────────────────────

    def close_position(
        self,
        account_id: str,
        ticker: str,
        quantity: int = 0,
        agent_name: str = "manual",
    ) -> bool:
        """Закрывает позицию по рыночной цене. quantity=0 → полное закрытие."""
        connector = self._connector()
        if not connector or not connector.is_connected():
            logger.warning(f'close_position({ticker}): коннектор не подключён, цена=н/д')
            return False

        pos = self.get_position(account_id, ticker)
        if not pos:
            logger.warning(f'close_position: позиция {ticker} не найдена на {account_id}, цена=н/д')
            return False

        total_qty = int(abs(float(pos.get('quantity', 0))))
        board = pos.get('board', 'TQBR')
        market_price = self._get_market_price_for_log(connector, ticker, board)
        if total_qty == 0:
            logger.warning(
                f'close_position: {ticker} — нулевая позиция, счёт={account_id}, цена~{market_price}'
            )
            return False

        close_qty = quantity if 0 < quantity <= total_qty else total_qty
        result = connector.close_position(
            account_id=account_id,
            ticker=ticker,
            quantity=close_qty,
            agent_name=agent_name,
        )
        if result:
            label = 'частично' if close_qty < total_qty else 'полностью'
            logger.info(
                f'Позиция {ticker} закрывается {label}: qty={close_qty}, '
                f'счёт={account_id}, цена~{market_price}'
            )
        else:
            logger.warning(
                f'close_position: не удалось отправить закрытие {ticker} '
                f'qty={close_qty}, счёт={account_id}, цена~{market_price}'
            )
        return result

    def close_all_positions(self, account_id: str, agent_name: str = "manual") -> int:
        """Закрывает все открытые позиции по счёту. Возвращает кол-во закрытых."""
        positions = self.get_positions(account_id)
        closed = 0
        for pos in positions:
            ticker = pos.get("ticker", "")
            qty = int(abs(float(pos.get("quantity", 0))))
            if qty > 0 and ticker:
                if self.close_position(account_id, ticker, agent_name=agent_name):
                    closed += 1
        logger.info(f"close_all_positions: закрыто {closed} позиций на {account_id}")
        return closed

    # ── Ручной ордер ──────────────────────────────────────────────────────

    def place_manual_order(
        self,
        account_id: str,
        ticker: str,
        side: str,
        quantity: int,
        order_type: str,
        price: float = 0.0,
    ) -> Optional[str]:
        """Выставляет ручной ордер. Возвращает order_id или None."""
        connector = self._connector()
        if not connector or not connector.is_connected():
            logger.warning('place_manual_order: коннектор не подключён, цена=н/д')
            return None

        price_text = (
            self._format_price(price)
            if order_type == 'limit'
            else self._get_market_price_for_log(connector, ticker)
        )
        order_id = connector.place_order(
            account_id=account_id,
            ticker=ticker,
            side=side,
            quantity=quantity,
            order_type=order_type,
            price=price,
            agent_name='manual',
        )
        if order_id:
            logger.info(
                f'Ручной ордер выставлен: {side.upper()} {ticker} x{quantity} '
                f'тип={order_type} цена={price_text} → {order_id}'
            )
        else:
            logger.warning(
                f'Ручной ордер отклонён: {side.upper()} {ticker} x{quantity} '
                f'тип={order_type} цена={price_text}'
            )
        return order_id

    # ── UI подписка ───────────────────────────────────────────────────────

    def on_update(self, callback: Callable):
        self._on_update_callbacks.append(callback)

    def remove_update_callback(self, callback: Callable):
        self._on_update_callbacks = [cb for cb in self._on_update_callbacks if cb != callback]


position_manager = PositionManager()
