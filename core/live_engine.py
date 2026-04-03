# core/live_engine.py

"""
LiveEngine — оркестратор реальной торговли.

Делегирует бизнес-логику компонентам:
- PositionTracker: управление позицией
- OrderExecutor: исполнение ордеров
- RiskGuard: circuit breaker и риск-лимиты
- TradeRecorder: запись сделок
- Reconciler: сверка позиции

Сохраняет обратную совместимость через прокси-свойства и методы.
"""

import math
import threading
import time
import traceback
import warnings
from datetime import datetime
from typing import Optional

import pandas as pd
from loguru import logger

from core.equity_tracker import flush_all as equity_flush_all, record_equity, get_max_drawdown
from config.settings import COMMISSION_FUTURES, COMMISSION_STOCK, TRADING_END_TIME_MIN
from core.commission_manager import commission_manager
from core.connector_manager import connector_manager
from core.instrument_classifier import instrument_classifier
from core.order_history import get_total_pnl, get_order_pairs
from core.position_tracker import PositionTracker
from core.order_executor import OrderExecutor
from core.risk_guard import RiskGuard
from core.trade_recorder import TradeRecorder
from core.reconciler import Reconciler
from core.finam_connector import FinamConnector
from core.quik_connector import QuikConnector

# Маппинг timeframe → строка для get_history
TIMEFRAME_TO_PERIOD = {
    "1": "1m", "5": "5m", "15": "15m", "30": "30m", "60": "1h",
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h",
}

# Маппинг timeframe → интервал поллинга (секунды)
TIMEFRAME_TO_POLL_SEC = {
    "1": 15, "5": 30, "15": 60, "30": 60, "60": 120,
    "1m": 15, "5m": 30, "15m": 60, "30m": 60, "1h": 120,
}


def _bar_from_row(row, dt: datetime) -> dict:
    """Конвертирует строку DataFrame в формат бара, совместимый с бэктестом."""
    return {
        "open": float(row["Open"]),
        "high": float(row["High"]),
        "low": float(row["Low"]),
        "close": float(row["Close"]),
        "vol": int(row.get("Volume", 0)),
        "dt": dt,
        "date_int": int(dt.strftime("%y%m%d")),
        "time_min": dt.hour * 60 + dt.minute,
        "weekday": dt.isoweekday(),  # 1=Пн..7=Вс
    }


class LiveEngine:
    """Движок реальной торговли. Один экземпляр на стратегию.

    Поллит get_history() с интервалом, зависящим от таймфрейма.
    При появлении нового закрытого бара — вызывает on_bar().

    Делегирует бизнес-логику компонентам:
    - position_tracker: управление позицией
    - order_executor: исполнение ордеров
    - risk_guard: circuit breaker и риск-лимиты
    - trade_recorder: запись сделок
    - reconciler: сверка позиции
    """

    def __init__(self, strategy_id: str, loaded_strategy, params: dict,
                 connector, account_id: str, ticker: str, board: str,
                 timeframe: str, agent_name: str = "", order_mode: str = "market",
                 lot_sizing: dict = None):
        self._strategy_id = strategy_id
        self._loaded = loaded_strategy
        self._module = loaded_strategy.module
        self._params = params
        self._connector = connector

        # Определяем ID коннектора по объекту
        found_id = next(
            (cid for cid, c in connector_manager.all().items() if c is connector),
            None
        )
        if found_id is None:
            if isinstance(connector, QuikConnector):
                self._connector_id = "quik"
            elif isinstance(connector, FinamConnector):
                self._connector_id = "finam"
            elif isinstance(connector, PaperConnector):
                self._connector_id = "paper"
            else:
                self._connector_id = "finam"
        else:
            self._connector_id = found_id

        self._account_id = account_id
        self._ticker = ticker
        self._board = board
        self._timeframe = timeframe
        self._agent_name = agent_name or strategy_id
        self._order_mode = order_mode
        self._lot_sizing = lot_sizing or {}

        # Комиссия: поддержка режима "auto" или ручных значений
        commission_param = params.get("commission", "auto")
        if commission_param == "auto":
            self._commission_mode = "auto"
            self._commission_pct = 0.0
            self._commission_rub = 0.0
        else:
            self._commission_mode = "manual"
            self._commission_pct = float(params.get("commission_pct", commission_param if isinstance(commission_param, (int, float)) else 0.0))
            self._commission_rub = float(params.get("commission_rub", commission_param if isinstance(commission_param, (int, float)) else 0.0))

        self._period_str = TIMEFRAME_TO_PERIOD.get(timeframe, "5m")
        self._poll_interval = TIMEFRAME_TO_POLL_SEC.get(timeframe, 30)
        self._fast_poll_interval = float(params.get("fast_poll_interval", 0.5))
        self._fast_poll_window = float(params.get("fast_poll_window", 10.0))
        self._last_signal_ts = 0.0

        # Bars storage
        self._bars: list[dict] = []
        self._bars_lock = threading.Lock()

        # Состояние поллинга
        self._running = False
        self._stop_event = threading.Event()
        self._last_bar_dt: Optional[datetime] = None
        self._thread: Optional[threading.Thread] = None
        self._subscribed_quotes = False

        # Счётчик тайм-аутов get_history
        self._consecutive_timeouts: int = 0
        self._MAX_CONSECUTIVE_TIMEOUTS = 5

        # Параметры для point_cost и lot_size
        self._last_price: float = 0.0
        self._point_cost: float = 1.0
        self._lot_size: int = 1

        # === Компоненты (делегирование бизнес-логики) ===

        # 1. PositionTracker
        self._position_tracker = PositionTracker()

        # 2. RiskGuard
        self._risk_guard = RiskGuard(
            strategy_id=self._strategy_id,
            circuit_breaker_threshold=3,
            circuit_breaker_timeout=60.0,
            max_position_size=int(params.get("max_position_size", 0) or 0),
            daily_loss_limit=float(params.get("daily_loss_limit", 0.0) or 0.0),
            get_total_pnl=get_total_pnl,
        )

        # 3. TradeRecorder
        self._trade_recorder = TradeRecorder(
            strategy_id=self._strategy_id,
            ticker=self._ticker,
            board=self._board,
            agent_name=self._agent_name,
            get_point_cost=lambda: self._point_cost,
            get_lot_size=lambda: self._lot_size,
            is_futures=lambda: self._is_futures(),
            calculate_commission=self._calculate_commission,
            get_last_price=lambda: self._last_price,
            get_position_qty=lambda: self._position_tracker.get_position_qty(),
            get_entry_price=lambda: self._position_tracker.get_entry_price(),
        )

        # 4. OrderExecutor
        self._order_executor = OrderExecutor(
            strategy_id=self._strategy_id,
            connector=self._connector,
            position_tracker=self._position_tracker,
            trade_recorder=self._trade_recorder,
            risk_guard=self._risk_guard,
            account_id=self._account_id,
            ticker=self._ticker,
            board=self._board,
            agent_name=self._agent_name,
            order_mode=self._order_mode,
            lot_sizing=self._lot_sizing,
            get_last_price=lambda: self._last_price,
            get_point_cost=lambda: self._point_cost,
            get_lot_size=lambda: self._lot_size,
            is_futures=lambda: self._is_futures(),
            calculate_commission=self._calculate_commission,
            on_reconcile=self._detect_position,
        )

        # 5. Reconciler
        self._reconciler = Reconciler(
            strategy_id=self._strategy_id,
            ticker=self._ticker,
            account_id=self._account_id,
            connector=self._connector,
            position_tracker=self._position_tracker,
            get_order_pairs=get_order_pairs,
            detect_position=self._detect_position,
            reconcile_interval_sec=60.0,
            alert_cooldown_sec=300.0,
        )

    # === Legacy-прокси для обратной совместимости ===

    @property
    def _position(self) -> int:
        """Прокси к position_tracker.get_position()."""
        return self._position_tracker.get_position()

    @_position.setter
    def _position(self, value: int):
        """Прокси к position_tracker.update_position()."""
        qty = self._position_tracker.get_position_qty()
        price = self._position_tracker.get_entry_price()
        self._position_tracker.update_position(value, qty, price)

    @property
    def _position_qty(self) -> int:
        return self._position_tracker.get_position_qty()

    @_position_qty.setter
    def _position_qty(self, value: int):
        pos = self._position_tracker.get_position()
        price = self._position_tracker.get_entry_price()
        self._position_tracker.update_position(pos, value, price)

    @property
    def _entry_price(self) -> float:
        return self._position_tracker.get_entry_price()

    @_entry_price.setter
    def _entry_price(self, value: float):
        pos = self._position_tracker.get_position()
        qty = self._position_tracker.get_position_qty()
        self._position_tracker.update_position(pos, qty, value)

    @property
    def _order_in_flight(self) -> bool:
        return self._position_tracker.is_order_in_flight()

    @_order_in_flight.setter
    def _order_in_flight(self, value: bool):
        self._position_tracker.set_order_in_flight(value)

    @property
    def _position_lock(self):
        return self._position_tracker._position_lock

    @property
    def _consecutive_failures(self) -> int:
        return self._risk_guard.consecutive_failures

    @_consecutive_failures.setter
    def _consecutive_failures(self, value: int):
        """Legacy setter — не используется в новом коде."""
        pass

    @property
    def _circuit_open(self) -> bool:
        return self._risk_guard.is_circuit_open()

    @property
    def _chase_lock(self):
        return self._order_executor._chase_lock

    @property
    def _active_chase_orders(self):
        return self._order_executor._active_chase_orders

    # === Вспомогательные методы ===

    def _is_futures(self, ticker: str = None) -> bool:
        t = ticker or self._ticker
        b = self._board if ticker is None else ""
        return instrument_classifier.is_futures(t, b)

    def _calculate_commission(self, ticker: str, qty: int, price: float, sec_type: str = None) -> float:
        """Рассчитывает комиссию за сделку."""
        abs_qty = abs(qty)
        if sec_type is None:
            sec_type = 'futures' if self._is_futures(ticker) else 'stock'

        if self._commission_mode == "auto":
            try:
                order_role = "maker" if self._order_mode == "limit" else "taker"
                commission = commission_manager.calculate(
                    ticker=ticker,
                    board=self._board,
                    quantity=abs_qty,
                    price=price,
                    order_role=order_role,
                    point_cost=self._point_cost,
                    connector_id=self._connector_id,
                    lot_size=self._lot_size,
                )
                return commission
            except Exception as e:
                logger.warning(
                    f"[LiveEngine:{self._strategy_id}] Ошибка автоматического расчёта комиссии: {e}. "
                    f"Используется fallback."
                )

        return self._calculate_commission_manual(ticker, abs_qty, price, sec_type)

    def _calculate_commission_manual(self, ticker: str, abs_qty: int, price: float, sec_type: str) -> float:
        """Рассчитывает комиссию вручную."""
        if sec_type == 'futures':
            commission_per_lot = self._commission_rub if self._commission_rub > 0 else COMMISSION_FUTURES
            return commission_per_lot * abs_qty
        else:
            commission_pct = self._commission_pct if self._commission_pct > 0 else COMMISSION_STOCK
            trade_value = price * abs_qty
            return trade_value * (commission_pct / 100.0)

    def _load_point_cost(self) -> bool:
        """Загружает стоимость пункта из коннектора с повторными попытками."""
        max_retries = 3 if self._is_futures(self._ticker) else 1
        retry_delay = 2

        for attempt in range(max_retries):
            if attempt > 0:
                logger.info(f'[LiveEngine:{self._strategy_id}] Повторная попытка {attempt + 1}/{max_retries} для point_cost...')
                time.sleep(retry_delay)

            try:
                if hasattr(self._connector, 'get_sec_info'):
                    sec_info = self._connector.get_sec_info(self._ticker, self._board)
                    if sec_info:
                        minstep = sec_info.get('minstep')
                        pc_from_connector = float(sec_info.get('point_cost') or 0.0)
                        lot_size = sec_info.get('lotsize') or sec_info.get('lot_size') or 0
                        try:
                            self._lot_size = max(int(lot_size), 1)
                        except (TypeError, ValueError):
                            self._lot_size = 0

                        # Приоритет 1: MOEX API
                        if hasattr(self._connector, 'get_moex_info'):
                            moex_sec_type = 'futures' if self._is_futures(self._ticker) else 'stock'
                            moex_info = self._connector.get_moex_info(self._ticker, sec_type=moex_sec_type)
                            if moex_info:
                                if moex_info.get('point_cost'):
                                    self._point_cost = float(moex_info['point_cost'])
                                if moex_info.get('lot_size'):
                                    self._lot_size = max(int(moex_info['lot_size']), 1)
                                logger.info(
                                    f'[LiveEngine:{self._strategy_id}] point_cost={self._point_cost} '
                                    f'lot_size={self._lot_size} (из MOEX API для {self._ticker})'
                                )
                                return True

                        # Приоритет 2: point_cost от коннектора
                        if pc_from_connector > 0:
                            self._point_cost = pc_from_connector
                            if self._lot_size <= 0:
                                self._lot_size = 1
                            logger.info(
                                f'[LiveEngine:{self._strategy_id}] point_cost={pc_from_connector}'
                                f' minstep={minstep}'
                                f' lot_size={self._lot_size}'
                                f" step_cost={round(pc_from_connector * float(minstep), 6) if minstep else 'n/a'}"
                            )
                            return True

                # Fallback: MOEX API для акций
                if not self._is_futures(self._ticker):
                    try:
                        from core.moex_api import MOEXClient
                        moex_info = MOEXClient.get_instrument_info(self._ticker, sec_type='stock')
                        if moex_info and moex_info.get('lot_size'):
                            self._lot_size = max(int(moex_info['lot_size']), 1)
                            self._point_cost = 1.0
                            logger.info(
                                f"[LiveEngine:{self._strategy_id}] "
                                f"lot_size={self._lot_size} из MOEX API (брокер недоступен)"
                            )
                            return True
                    except Exception as e:
                        logger.debug(f"[LiveEngine:{self._strategy_id}] MOEX fallback error: {e}")

            except Exception as e:
                logger.warning(f"[LiveEngine:{self._strategy_id}] point_cost error (attempt {attempt + 1}): {e}")
                self._point_cost = 0.0

        logger.error(f"[LiveEngine:{self._strategy_id}] point_cost не получен после {max_retries} попыток — старт запрещён")
        self._point_cost = 0.0
        return False

    # === Публичные API ===

    @property
    def is_running(self) -> bool:
        return self._running

    def get_position_info(self) -> dict:
        """Возвращает позицию агента для UI."""
        price = self._get_realtime_price()
        qty = self._position_tracker.get_position_qty()

        if qty == 0:
            return {
                "ticker": self._ticker,
                "quantity": 0,
                "side": "",
                "avg_price": 0.0,
                "current_price": price,
                "pnl": 0.0,
            }

        entry_price = self._position_tracker.get_entry_price()
        point_cost = self._point_cost

        gross_pnl = (price - entry_price) * qty * point_cost
        entry_commission = self._calculate_commission(self._ticker, qty, entry_price)
        exit_commission = self._calculate_commission(self._ticker, qty, price)
        net_pnl = gross_pnl - entry_commission - exit_commission

        return {
            "ticker": self._ticker,
            "quantity": qty,
            "side": "buy" if qty > 0 else "sell",
            "avg_price": entry_price,
            "current_price": price,
            "pnl": round(net_pnl, 2),
        }

    def _get_realtime_price(self) -> float:
        """Возвращает актуальную цену."""
        try:
            if hasattr(self._connector, "get_best_quote"):
                quote = self._connector.get_best_quote(self._board, self._ticker)
                if quote:
                    last = quote.get("last", 0)
                    if last:
                        self._last_price = last
                        return self._last_price
                    bid = quote.get("bid", 0)
                    offer = quote.get("offer", 0)
                    if bid and offer:
                        self._last_price = (bid + offer) / 2
                        return self._last_price
                    if bid:
                        self._last_price = bid
                        return self._last_price
                    if offer:
                        self._last_price = offer
                        return self._last_price
        except Exception:
            pass
        return self._last_price

    def _record_equity(self):
        """Записывает текущий equity."""
        try:
            realized = get_total_pnl(self._strategy_id) or 0.0
            unrealized = 0.0

            position_qty = self._position_tracker.get_position_qty()
            last_price = self._last_price
            entry_price = self._position_tracker.get_entry_price()

            if position_qty and last_price and entry_price:
                gross_unrealized = (last_price - entry_price) * position_qty * self._point_cost
                entry_commission = self._calculate_commission(self._ticker, position_qty, entry_price)
                exit_commission = self._calculate_commission(self._ticker, position_qty, last_price)
                unrealized = gross_unrealized - entry_commission - exit_commission

            equity = realized + unrealized
            record_equity(self._strategy_id, equity, position_qty or 0)
        except Exception as e:
            logger.debug(f"[LiveEngine:{self._strategy_id}] equity_tracker error: {e}")

    # === Start / Stop ===

    def start(self):
        """Запускает daemon-поток поллинга."""
        if self._running:
            return
        logger.info(f"[LiveEngine:{self._strategy_id}] Запуск: {self._ticker} "
                     f"tf={self._timeframe} board={self._board} poll={self._poll_interval}s")

        self._subscribe_quotes()
        point_cost_ok = self._load_point_cost()

        is_fut = instrument_classifier.is_futures(self._ticker, self._board)
        if is_fut and not point_cost_ok:
            logger.error(
                f"[LiveEngine:{self._strategy_id}] "
                f"Запуск ОТМЕНЁН: не удалось получить point_cost для фьючерса "
                f"{self._ticker} ({self._board}). "
                f"Проверьте подключение к брокеру и корректность тикера."
            )
            try:
                from core.telegram_bot import notifier, EventCode
                notifier.send(
                    EventCode.STRATEGY_ERROR,
                    agent=self._strategy_id,
                    description=f"Не удалось получить point_cost для {self._ticker}. Запуск отменён.",
                )
            except Exception:
                pass
            return

        if not point_cost_ok and not is_fut:
            logger.warning(
                f"[LiveEngine:{self._strategy_id}] "
                f"point_cost не загружен для {self._ticker}, используется 1.0"
            )
            self._point_cost = 1.0

        self._connector.on_reconnect(self._on_connector_reconnect)

        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True,
                                        name=f"LiveEngine-{self._strategy_id}")
        self._thread.start()
        logger.info(f"[LiveEngine:{self._strategy_id}] Запущен, позиция={self._position_tracker.get_position()}")

    def stop(self, close_position: bool = None):
        """Останавливает поток поллинга и выполняет graceful shutdown."""
        if not self._running:
            return

        should_close = close_position
        if should_close is None:
            from core.storage import get_strategy
            data = get_strategy(self._strategy_id) or {}
            should_close = data.get("close_position_on_stop", False)

        if should_close:
            qty = self._position_tracker.get_position_qty()
            pos = self._position_tracker.get_position()
            if qty != 0 and pos != 0:
                logger.info(
                    f"[LiveEngine:{self._strategy_id}] "
                    f"Закрытие позиции перед остановкой (close_position_on_stop=True)"
                )
                self._emergency_close_position()

        self._running = False
        self._stop_event.set()

        # Останавливаем компоненты
        self._order_executor.stop()

        # Ждём завершения основного потока
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

        self._unsubscribe_quotes()
        equity_flush_all()

        logger.info(f"[LiveEngine:{self._strategy_id}] Остановлен")

    def _subscribe_quotes(self):
        """Подписывается на котировки тикера."""
        try:
            if hasattr(self._connector, "subscribe_quotes"):
                self._connector.subscribe_quotes(self._board, self._ticker)
                self._subscribed_quotes = True
        except Exception as e:
            logger.warning(f"[LiveEngine:{self._strategy_id}] Ошибка подписки на котировки: {e}")

    def _unsubscribe_quotes(self):
        """Отписывается от котировок тикера."""
        try:
            if self._subscribed_quotes and hasattr(self._connector, "unsubscribe_quotes"):
                self._connector.unsubscribe_quotes(self._board, self._ticker)
                self._subscribed_quotes = False
        except Exception as e:
            logger.warning(f"[LiveEngine:{self._strategy_id}] Ошибка отписки от котировок: {e}")

    def _on_connector_reconnect(self):
        """Обработчик переподключения к бирже."""
        logger.info(f"[{self._strategy_id}] Переподключение к бирже, синхронизация позиции...")
        self._detect_position()
        logger.info(f"[{self._strategy_id}] Позиция после синхронизации: {self._position_tracker.get_position()}")

    # === Позиция и reconciliation ===

    def _detect_position(self):
        """Определяет текущую позицию по тикеру из коннектора."""
        try:
            positions = self._connector.get_positions(self._account_id)
            new_position = 0
            new_qty = 0
            new_entry_price = 0.0
            new_last_price = 0.0

            for pos in positions:
                if pos.get("ticker") == self._ticker:
                    qty = float(pos.get("quantity", 0))
                    new_qty = int(qty)
                    entry_price = float(pos.get("avg_price", 0))
                    if not entry_price:
                        entry_price = self._get_entry_price_from_history()
                    new_entry_price = entry_price
                    new_last_price = float(pos.get("current_price", 0))
                    if qty > 0:
                        new_position = 1
                    elif qty < 0:
                        new_position = -1
                    else:
                        new_position = 0
                    logger.info(f"[LiveEngine:{self._strategy_id}] "
                                f"Позиция из коннектора: {new_position} (qty={qty}) "
                                f"entry_price={new_entry_price}")
                    break

            self._position_tracker.update_position(new_position, new_qty, new_entry_price)
            self._last_price = new_last_price
        except Exception as e:
            logger.warning(f"[LiveEngine:{self._strategy_id}] "
                           f"Не удалось определить позицию: {e}")
            self._position_tracker.update_position(0, 0, 0.0)
            self._last_price = 0.0
            raise RuntimeError("Position detection failed — reconcile required")

    def _get_entry_price_from_history(self) -> float:
        """Берёт цену входа из последней незакрытой пары ордеров."""
        try:
            pairs = get_order_pairs(self._strategy_id)
            for pair in reversed(pairs):
                if pair["close"] is None and pair["open"]:
                    return float(pair["open"].get("price", 0))
        except Exception:
            pass
        return 0.0

    def _emergency_close_position(self):
        """Экстренное закрытие позиции при circuit breaker."""
        qty = self._position_tracker.get_position_qty()
        pos = self._position_tracker.get_position()

        if qty == 0 or pos == 0:
            return

        close_side = "sell" if pos == 1 else "buy"
        abs_qty = abs(qty)
        logger.warning(
            f"[LiveEngine:{self._strategy_id}] "
            f"АВАРИЙНОЕ ЗАКРЫТИЕ: {close_side.upper()} {self._ticker} x{abs_qty}"
        )
        try:
            tid = self._connector.place_order(
                account_id=self._account_id,
                ticker=self._ticker,
                side=close_side,
                quantity=abs_qty,
                order_type="market",
                board=self._board,
                agent_name=self._agent_name,
            )
            if tid:
                logger.warning(
                    f"[LiveEngine:{self._strategy_id}] "
                    f"Аварийный ордер принят: tid={tid}"
                )
            else:
                logger.error(
                    f"[LiveEngine:{self._strategy_id}] "
                    f"Аварийный ордер ОТКЛОНЁН брокером — позиция остаётся открытой!"
                )
        except Exception as e:
            logger.error(
                f"[LiveEngine:{self._strategy_id}] "
                f"Ошибка аварийного закрытия: {e} — позиция остаётся открытой!"
            )

        try:
            from core.telegram_bot import notifier, EventCode
            notifier.send(
                EventCode.STRATEGY_CRASHED,
                agent=self._strategy_id,
                description=(
                    f"Circuit breaker сработал после {self._risk_guard.circuit_breaker_threshold} ошибок. "
                    f"Попытка закрыть позицию {close_side.upper()} {self._ticker} x{abs_qty}."
                ),
                traceback="",
            )
        except Exception:
            pass

    # === Poll loop ===

    def _get_lookback(self) -> int:
        if hasattr(self._module, "get_lookback"):
            try:
                return int(self._module.get_lookback(self._params))
            except Exception:
                pass
        return 300

    def _poll_loop(self):
        """Основной цикл: загрузка истории → поллинг новых баров."""
        self._load_and_update()
        if not self._position_tracker.get_entry_price():
            self._detect_position()

        while not self._stop_event.is_set():
            now = time.monotonic()
            interval = self._poll_interval
            if now - self._last_signal_ts <= self._fast_poll_window:
                interval = min(interval, self._fast_poll_interval)

            self._stop_event.wait(interval)
            if self._stop_event.is_set():
                break
            if not self._connector.is_connected():
                continue
            try:
                self._load_and_update()
                self._reconciler.reconcile()
            except Exception as e:
                logger.error(f"[LiveEngine:{self._strategy_id}] Ошибка в poll_loop: {e}\n"
                             f"{traceback.format_exc()}")

    def _load_and_update(self):
        """Загружает историю, ищет новые бары, вызывает on_bar при необходимости."""
        lookback = self._get_lookback()
        days = max(lookback // 50, 5)

        if self._stop_event.is_set():
            return

        if self._last_bar_dt is None:
            try:
                if hasattr(self._connector, "clear_history_cache"):
                    self._connector.clear_history_cache(self._ticker, self._board)
            except Exception:
                pass

        result = {'df': None, 'error': None}

        def _fetch_history():
            try:
                result['df'] = self._connector.get_history(
                    ticker=self._ticker,
                    board=self._board,
                    period=self._period_str,
                    days=days,
                )
            except Exception as e:
                result['error'] = e

        fetch_thread = threading.Thread(target=_fetch_history, daemon=True)
        fetch_thread.start()

        connector_id = getattr(self._connector, '_connector_id', 'finam')
        timeout = 30 if connector_id == 'quik' else 10
        fetch_thread.join(timeout=timeout)

        if fetch_thread.is_alive():
            self._consecutive_timeouts += 1
            logger.warning(
                f"[LiveEngine:{self._strategy_id}] get_history тайм-аут "
                f"({self._consecutive_timeouts}/{self._MAX_CONSECUTIVE_TIMEOUTS}), пропускаем тик"
            )
            if self._consecutive_timeouts >= self._MAX_CONSECUTIVE_TIMEOUTS:
                logger.error(
                    f"[LiveEngine:{self._strategy_id}] {self._MAX_CONSECUTIVE_TIMEOUTS} "
                    f"тайм-аутов подряд — остановка стратегии"
                )
                self._emergency_close_position()
                self.stop()
            return
        else:
            self._consecutive_timeouts = 0

        if result['error']:
            logger.error(
                f"[LiveEngine:{self._strategy_id}] Ошибка get_history: {result['error']}"
            )
            return

        df = result['df']
        if df is None or df.empty:
            return

        bars = []
        for dt_idx, row in df.iterrows():
            dt = dt_idx.to_pydatetime() if hasattr(dt_idx, 'to_pydatetime') else dt_idx
            bars.append(_bar_from_row(row, dt))

        if not bars:
            return

        newest_dt = bars[-1]["dt"]
        if self._last_bar_dt and newest_dt <= self._last_bar_dt:
            return

        self._last_price = bars[-1]["close"]

        if self._point_cost == 1.0:
            self._load_point_cost()

        self._record_equity()

        new_bar_dt = bars[-1]["dt"]

        if self._last_bar_dt is None:
            with self._bars_lock:
                self._bars = bars
                self._last_bar_dt = new_bar_dt
            logger.info(f"[LiveEngine:{self._strategy_id}] Загружено {len(bars)} баров, "
                        f"последний: {new_bar_dt}")
            return

        if new_bar_dt <= self._last_bar_dt:
            return

        with self._bars_lock:
            self._bars = bars
            self._last_bar_dt = new_bar_dt

        logger.debug(f"[LiveEngine:{self._strategy_id}] Новый бар: {new_bar_dt} "
                      f"O={bars[-1]['open']} H={bars[-1]['high']} "
                      f"L={bars[-1]['low']} C={bars[-1]['close']}")
        self._process_bar()

    def _process_bar(self):
        """Пересчитывает индикаторы, вызывает on_bar(), исполняет сигнал."""
        with self._bars_lock:
            bars = list(self._bars)

        if len(bars) < 2:
            return
        closed_bars = bars[:-1]

        try:
            df = pd.DataFrame(closed_bars)
            if hasattr(self._module, "on_precalc"):
                df = self._module.on_precalc(df, self._params)

            processed_bars = df.to_dict("records")

            lookback = self._get_lookback()
            if len(processed_bars) > lookback:
                processed_bars = processed_bars[-lookback:]
        except Exception as e:
            logger.error(f"[LiveEngine:{self._strategy_id}] Ошибка precalc: {e}\n"
                         f"{traceback.format_exc()}")
            return

        if len(processed_bars) < min(10, self._get_lookback()):
            logger.warning(f"[LiveEngine:{self._strategy_id}] Истории недостаточно (bars={len(processed_bars)}), сигнал пропущен")
            return

        current_position = self._position_tracker.get_position()
        signal = self._loaded.call_on_bar(processed_bars, current_position, self._params)

        action = signal.get("action") if signal else None
        if action:
            self._last_signal_ts = time.monotonic()

            # Защита от двойного входа
            if action in ("buy", "sell"):
                if self._position_tracker.is_in_position():
                    logger.warning(
                        f"[LiveEngine:{self._strategy_id}] Позиция уже открыта "
                        f"({self._position_tracker.get_position()}, "
                        f"qty={self._position_tracker.get_position_qty()}), "
                        f"игнорируем {action.upper()}"
                    )
                    return

            logger.info(f"[LiveEngine:{self._strategy_id}] Сигнал: {signal}")

            # Делегируем execute_signal стратегии или order_executor
            if hasattr(self._module, "execute_signal"):
                try:
                    allowed_actions = {"buy", "sell", "close"}
                    signal_action = signal.get("action")
                    if signal_action not in allowed_actions:
                        logger.warning(
                            f"[LiveEngine:{self._strategy_id}] Некорректный action={signal_action!r} "
                            f"в сигнале custom execute_signal, сигнал пропущен"
                        )
                        return
                    try:
                        signal_qty = int(signal.get("qty", 1))
                    except (TypeError, ValueError):
                        logger.warning(
                            f"[LiveEngine:{self._strategy_id}] Некорректный qty в сигнале "
                            f"custom execute_signal: {signal.get('qty')!r}"
                        )
                        return
                    if signal_qty <= 0:
                        logger.warning(
                            f"[LiveEngine:{self._strategy_id}] qty <= 0 в сигнале "
                            f"custom execute_signal: {signal_qty}"
                        )
                        return

                    self._params["_strategy_id"] = self._strategy_id
                    self._params["_connector_id"] = self._connector_id
                    self._module.execute_signal(
                        signal, self._connector, self._params, self._account_id
                    )
                except Exception as e:
                    logger.error(f"[LiveEngine:{self._strategy_id}] "
                                 f"execute_signal error: {e}\n{traceback.format_exc()}")
            else:
                # Делегируем order_executor
                self._order_executor.execute_signal(signal)

    # === Legacy прокси-методы для обратной совместимости ===

    def _record_failure(self):
        """Прокси к risk_guard.record_failure()."""
        if self._risk_guard.record_failure():
            logger.error(
                f"[LiveEngine:{self._strategy_id}] CIRCUIT BREAKER: "
                f"{self._risk_guard.consecutive_failures} ошибок подряд — аварийное закрытие позиции"
            )
            self._emergency_close_position()
            self.stop()

    def _record_success(self):
        """Прокси к risk_guard.record_success()."""
        self._risk_guard.record_success()

    def _check_risk_limits(self, action: str, qty: int) -> tuple[bool, str]:
        """Прокси к risk_guard.check_risk_limits()."""
        return self._risk_guard.check_risk_limits(action, qty)

    def _record_trade(self, side: str, qty: int, price: float, comment: str,
                      order_type: str = "market", order_ref: str = ""):
        """Прокси к trade_recorder.record_trade()."""
        self._trade_recorder.record_trade(side, qty, price, comment, order_type, order_ref)

    def _execute_signal(self, signal: dict):
        """Прокси к order_executor.execute_signal()."""
        self._order_executor.execute_signal(signal)

    def _execute_chase(self, side: str, qty: int, comment: str, is_close: bool = False):
        """Прокси к order_executor._execute_chase()."""
        self._order_executor._execute_chase(side, qty, comment, is_close)

    def _on_chase_done(self, chase, side: str, qty: int, comment: str, is_close: bool, chase_ref: str = ""):
        """Прокси к order_executor._on_chase_done()."""
        self._order_executor._on_chase_done(chase, side, qty, comment, is_close, chase_ref)

    def _execute_market(self, side: str, qty: int, comment: str, fill_price: float):
        """Прокси к order_executor._execute_market()."""
        self._order_executor._execute_market(side, qty, comment, fill_price)

    def _execute_market_close(self, close_side: str, close_qty: int, comment: str, fill_price: float):
        """Прокси к order_executor._execute_market_close()."""
        self._order_executor._execute_market_close(close_side, close_qty, comment, fill_price)

    def _execute_limit_price(self, side: str, qty: int, comment: str, price: float, is_close: bool = False):
        """Прокси к order_executor._execute_limit_price()."""
        self._order_executor._execute_limit_price(side, qty, comment, price, is_close)

    def _calc_dynamic_qty(self, side: str):
        """Прокси к order_executor._calc_dynamic_qty()."""
        return self._order_executor._calc_dynamic_qty(side)

    def _maybe_reconcile(self):
        """Прокси к reconciler.reconcile()."""
        self._reconciler.reconcile()

    def _get_open_qty_from_history(self) -> int:
        """Прокси к reconciler.get_history_qty()."""
        return self._reconciler.get_history_qty()

    def _send_reconcile_alert(self, message: str):
        """Прокси к reconciler.send_alert()."""
        self._reconciler.send_alert(message)

    def __repr__(self):
        return (f"<LiveEngine {self._strategy_id} ticker={self._ticker} "
                f"pos={self._position_tracker.get_position()} running={self._running}>")
