# core/live_engine.py

import math
import threading
import time
import traceback
from datetime import datetime
from typing import Optional

import pandas as pd
from loguru import logger

from core.equity_tracker import flush_all as equity_flush_all, record_equity, get_max_drawdown
from config.settings import COMMISSION_FUTURES, COMMISSION_STOCK, TRADING_END_TIME_MIN
from core.commission_manager import commission_manager
from core.connector_manager import connector_manager
from core.instrument_classifier import instrument_classifier
from core.order_history import get_total_pnl, get_order_pairs, make_order, save_order
from core.storage import append_trade
from core.telegram_bot import notifier, EventCode
from core.chase_order import ChaseOrder
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
        # Сначала ищем в зарегистрированных коннекторах
        found_id = next(
            (cid for cid, c in connector_manager.all().items() if c is connector),
            None
        )
        if found_id is None:
            # Fallback: определяем по типу объекта
            if isinstance(connector, QuikConnector):
                self._connector_id = "quik"
            elif isinstance(connector, FinamConnector):
                self._connector_id = "finam"
            else:
                self._connector_id = "finam"  # значение по умолчанию
        else:
            self._connector_id = found_id
        
        self._account_id = account_id
        self._ticker = ticker
        self._board = board
        self._timeframe = timeframe
        self._agent_name = agent_name or strategy_id
        self._order_mode = order_mode  # "market" | "limit"
        self._lot_sizing = lot_sizing or {}  # {dynamic, lot, instances, drawdown}
        
        # Комиссия: поддержка режима "auto" или ручных значений
        commission_param = params.get("commission", "auto")
        if commission_param == "auto":
            self._commission_mode = "auto"
            self._commission_pct = 0.0
            self._commission_rub = 0.0
        else:
            self._commission_mode = "manual"
            # Для обратной совместимости: если есть старые параметры commission_pct/commission_rub
            self._commission_pct = float(params.get("commission_pct", commission_param if isinstance(commission_param, (int, float)) else 0.0))
            self._commission_rub = float(params.get("commission_rub", commission_param if isinstance(commission_param, (int, float)) else 0.0))

        self._period_str = TIMEFRAME_TO_PERIOD.get(timeframe, "5m")
        self._poll_interval = TIMEFRAME_TO_POLL_SEC.get(timeframe, 30)

        self._bars: list[dict] = []
        self._bars_lock = threading.Lock()
        self._position_lock = threading.Lock()  # защита от race condition между потоками
        self._position: int = 0
        self._position_qty: int = 0       # кол-во контрактов (со знаком)
        self._entry_price: float = 0.0    # цена входа
        self._last_price: float = 0.0     # последняя цена (из последнего бара)
        self._point_cost: float = 1.0     # стоимость пункта в рублях
        self._lot_size: int = 1           # размер лота для акций/ETF/облигаций
        self._running = False
        self._stop_event = threading.Event()
        self._last_bar_dt: Optional[datetime] = None
        self._thread: Optional[threading.Thread] = None
        self._subscribed_quotes = False
        self._active_chase_orders: list = []  # активные chase-ордера для graceful shutdown
        self._chase_lock = threading.Lock()    # защита списка chase-ордеров

        # Флаг активного лимитного ордера (limit/limit_price) — не блокирует poll_loop
        self._order_in_flight: bool = False

        # Circuit breaker для ошибок коннектора
        self._consecutive_failures: int = 0   # счётчик подряд идущих ошибок
        self._last_failure_time: float = 0.0   # время последней ошибки
        self._CIRCUIT_BREAKER_THRESHOLD = 3   # порог для остановки
        self._CIRCUIT_BREAKER_TIMEOUT = 60.0   # таймаут сброса счётчика (сек)

    def _record_failure(self):
        """Регистрирует ошибку для circuit breaker."""
        now = time.monotonic()
        if now - self._last_failure_time < self._CIRCUIT_BREAKER_TIMEOUT:
            self._consecutive_failures += 1
        else:
            self._consecutive_failures = 1
        self._last_failure_time = now

        if self._consecutive_failures >= self._CIRCUIT_BREAKER_THRESHOLD:
            logger.error(
                f"[LiveEngine:{self._strategy_id}] CIRCUIT BREAKER: "
                f"{self._consecutive_failures} ошибок подряд, стратегия остановлена"
            )
            self.stop()

    def _record_success(self):
        """Сбрасывает счётчик ошибок при успешной операции."""
        self._consecutive_failures = 0

    def _is_futures(self, ticker: str = None) -> bool:
        """Определяет, является ли инструмент фьючерсом через instrument_classifier."""
        t = ticker or self._ticker
        b = self._board if ticker is None else ""
        return instrument_classifier.is_futures(t, b)

    def _calculate_commission_manual(self, ticker: str, abs_qty: int, price: float, sec_type: str) -> float:
        """Рассчитывает комиссию вручную по константам или пользовательским параметрам.

        Используется как основной путь в manual-режиме и как fallback в auto-режиме
        при недоступности commission_manager.

        Args:
            ticker: Тикер инструмента (для логирования)
            abs_qty: Количество лотов/контрактов (всегда положительное)
            price: Цена исполнения
            sec_type: Тип инструмента — 'futures' или 'stock'

        Returns:
            Комиссия в рублях (всегда положительная)
        """
        if sec_type == 'futures':
            commission_per_lot = self._commission_rub if self._commission_rub > 0 else COMMISSION_FUTURES
            commission = commission_per_lot * abs_qty
            logger.debug(
                f"[LiveEngine:{self._strategy_id}] Комиссия (фьючерс, ручная): "
                f"{commission:.2f} руб ({commission_per_lot} руб/лот * {abs_qty} лот)"
            )
        else:
            commission_pct = self._commission_pct if self._commission_pct > 0 else COMMISSION_STOCK
            trade_value = price * abs_qty
            commission = trade_value * (commission_pct / 100.0)
            logger.debug(
                f"[LiveEngine:{self._strategy_id}] Комиссия (акция, ручная): "
                f"{commission:.2f} руб ({commission_pct}% от {trade_value:.2f} руб)"
            )
        return commission

    def _calculate_commission(self, ticker: str, qty: int, price: float, sec_type: str = None) -> float:
        """Рассчитывает комиссию за сделку.

        В auto-режиме делегирует commission_manager; при ошибке падает на ручной расчёт.
        В manual-режиме использует параметры стратегии или константы из config.

        Args:
            ticker: Тикер инструмента
            qty: Количество лотов/контрактов (может быть отрицательным)
            price: Цена исполнения
            sec_type: Тип инструмента ('futures' или 'stock'), определяется автоматически если None

        Returns:
            Комиссия в рублях (всегда положительная)
        """
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
                logger.debug(
                    f"[LiveEngine:{self._strategy_id}] Комиссия (авто): "
                    f"{commission:.2f} руб для {ticker} ({abs_qty} лот, {order_role})"
                )
                return commission
            except Exception as e:
                logger.warning(
                    f"[LiveEngine:{self._strategy_id}] Ошибка автоматического расчёта комиссии: {e}. "
                    f"Используется fallback."
                )

        return self._calculate_commission_manual(ticker, abs_qty, price, sec_type)

    def _load_point_cost(self):
        """Загружает стоимость пункта из коннектора.

        TRANSAQ семантика (документация, раздел 4.6):
          point_cost = стоимость изменения цены на 1.0 (один пункт) за 1 контракт, руб.
          step_cost  = point_cost * minstep  (стоимость минимального шага цены)

        Формула PnL: (price - entry_price) * qty * point_cost
        Это корректно, т.к. (price - entry_price) уже выражено в пунктах.

        Приоритет:
          1. Если коннектор поддерживает get_moex_info — пробуем получить точное значение с MOEX API.
          2. Иначе — берём point_cost напрямую от коннектора.
         """
        try:
            if hasattr(self._connector, 'get_sec_info'):
               sec_info = self._connector.get_sec_info(self._ticker, self._board)
               if sec_info:
                    minstep = sec_info.get('minstep')
                    pc_from_connector = float(sec_info.get('point_cost') or 1.0)
                    lot_size = sec_info.get('lotsize') or sec_info.get('lot_size') or 1
                    try:
                        self._lot_size = max(int(lot_size), 1)
                    except (TypeError, ValueError):
                        self._lot_size = 1

                    # Приоритет 1: попробуем получить точное значение с MOEX API
                    if hasattr(self._connector, 'get_moex_info'):
                        moex_sec_type = 'futures' if self._is_futures(self._ticker) else 'stock'
                        moex_info = self._connector.get_moex_info(self._ticker, sec_type=moex_sec_type)
                        if moex_info:
                            if moex_info.get('point_cost'):
                                self._point_cost = moex_info['point_cost']
                            if moex_info.get('lot_size'):
                                self._lot_size = max(int(moex_info['lot_size']), 1)
                            logger.info(
                                f'[LiveEngine:{self._strategy_id}] point_cost={self._point_cost} '
                                f'lot_size={self._lot_size} (из MOEX API для {self._ticker})'
                            )
                            return

                    # Приоритет 2: point_cost от коннектора
                    if pc_from_connector > 0:
                        self._point_cost = pc_from_connector
                        logger.info(
                            f'[LiveEngine:{self._strategy_id}] point_cost={pc_from_connector}'
                            f' minstep={minstep}'
                            f' lot_size={self._lot_size}'
                            f" step_cost={round(pc_from_connector * float(minstep), 6) if minstep else 'n/a'}"
                        )
        except Exception as e:
           logger.warning(f"[LiveEngine:{self._strategy_id}] point_cost error: {e}")

    @property
    def is_running(self) -> bool:
        return self._running

    def get_position_info(self) -> dict:
        """Возвращает позицию агента для UI.

        pnl = плавающий gross PnL - комиссия за вход (одна сторона уже уплачена).
        Комиссия за выход будет вычтена при закрытии в get_order_pairs().
        Здесь вычитаем только комиссию за вход, чтобы не завышать unrealized PnL.
        """
        price = self._get_realtime_price()
        qty = self._position_qty
        
        if qty == 0:
            return {
                "ticker": self._ticker,
                "quantity": 0,
                "side": "",
                "avg_price": 0.0,
                "current_price": price,
                "pnl": 0.0,
            }
        
        # Расчет gross PnL (без комиссий)
        gross_pnl = (price - self._entry_price) * qty * self._point_cost
        
        # Комиссия за вход (используем новый метод расчета)
        entry_commission = self._calculate_commission(self._ticker, qty, self._entry_price)
        
        # Комиссия за выход (прогнозируемая, по текущей цене)
        exit_commission = self._calculate_commission(self._ticker, qty, price)
        
        # Net PnL = gross PnL - комиссия за вход - комиссия за выход
        net_pnl = gross_pnl - entry_commission - exit_commission
        
        logger.debug(
            f"[LiveEngine:{self._strategy_id}] Unrealized PnL: "
            f"gross={gross_pnl:.2f}, entry_comm={entry_commission:.2f}, "
            f"exit_comm={exit_commission:.2f}, net={net_pnl:.2f}"
        )
        
        return {
            "ticker": self._ticker,
            "quantity": qty,
            "side": "buy" if qty > 0 else "sell",
            "avg_price": self._entry_price,
            "current_price": price,
            "pnl": round(net_pnl, 2),
        }

    def _get_realtime_price(self) -> float:
        """Возвращает актуальную цену: last из котировок, затем bid/offer, затем последний бар."""
        try:
            if hasattr(self._connector, "get_best_quote"):
                quote = self._connector.get_best_quote(self._board, self._ticker)
                if quote:
                    # Приоритет: last > mid(bid+offer) > bid > offer
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
        """Записывает текущий equity для трекинга реальной просадки.

        equity = реализованный P/L (закрытые сделки) + плавающий P/L (открытая позиция).
        Учитывает комиссии при расчете unrealized PnL.
        """
        try:
            realized = get_total_pnl(self._strategy_id) or 0.0
            unrealized = 0.0
            
            if self._position_qty and self._last_price and self._entry_price:
                # Gross PnL
                gross_unrealized = (self._last_price - self._entry_price) * self._position_qty * self._point_cost
                
                # Комиссия за вход и выход
                entry_commission = self._calculate_commission(self._ticker, self._position_qty, self._entry_price)
                exit_commission = self._calculate_commission(self._ticker, self._position_qty, self._last_price)
                
                # Net unrealized PnL с учетом комиссий
                unrealized = gross_unrealized - entry_commission - exit_commission
                
                logger.debug(
                    f"[LiveEngine:{self._strategy_id}] Equity: realized={realized:.2f}, "
                    f"unrealized={unrealized:.2f} (gross={gross_unrealized:.2f}, "
                    f"comm={entry_commission + exit_commission:.2f})"
                )

            equity = realized + unrealized
            record_equity(self._strategy_id, equity, self._position_qty or 0)
        except Exception as e:
            logger.debug(f"[LiveEngine:{self._strategy_id}] equity_tracker error: {e}")

    def start(self):
        """Запускает daemon-поток поллинга."""
        if self._running:
            return
        logger.info(f"[LiveEngine:{self._strategy_id}] Запуск: {self._ticker} "
                     f"tf={self._timeframe} board={self._board} poll={self._poll_interval}s")

        self._subscribe_quotes()
        self._load_point_cost()
        
        # Подписываемся на событие переподключения
        self._connector.on_reconnect(self._on_connector_reconnect)
        
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True,
                                        name=f"LiveEngine-{self._strategy_id}")
        self._thread.start()
        logger.info(f"[LiveEngine:{self._strategy_id}] Запущен, позиция={self._position}")

    def stop(self):
        """Останавливает поток поллинга и выполняет graceful shutdown."""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()

        # Graceful shutdown: отменяем активные chase-ордера
        # Копируем список ВНЕ блокировки, чтобы избежать deadlock
        with self._chase_lock:
            active_chases = list(self._active_chase_orders)
        
        # Отменяем chase-потоки без удержания блокировки
        for chase in active_chases:
            if not chase.is_done:
                chase.cancel()
                chase.wait(timeout=5)
        
        # Очищаем список под блокировкой после завершения всех потоков
        with self._chase_lock:
            self._active_chase_orders.clear()

        # Ждём завершения основного потока
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

        self._unsubscribe_quotes()

        # Flush equity tracker для сохранения накопленных данных
        equity_flush_all()

        logger.info(f"[LiveEngine:{self._strategy_id}] Остановлен")

    def _subscribe_quotes(self):
        """Подписывается на котировки тикера для обновления цены в реальном времени."""
        try:
            if hasattr(self._connector, "subscribe_quotes"):
                self._connector.subscribe_quotes(self._board, self._ticker)
                self._subscribed_quotes = True
                logger.debug(f"[LiveEngine:{self._strategy_id}] Подписка на котировки {self._ticker}")
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
        
        # Синхронизируем позицию с биржей
        self._detect_position()
        
        logger.info(f"[{self._strategy_id}] Позиция после синхронизации: {self._position}")

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
                    # TransAQ не отдаёт avg_price — берём из order_history
                    entry_price = float(pos.get("avg_price", 0))
                    if not entry_price:
                        entry_price = self._get_entry_price_from_history()
                        # ВАЛИДАЦИЯ: проверяем расхождение между коннектором и order_history
                        if qty > 0 and entry_price == 0.0:
                            logger.warning(
                                f"[LiveEngine:{self._strategy_id}] "
                                f"РАСХОЖДЕНИЕ: коннектор показывает qty={qty}, "
                                f"но order_history не содержит позиции! "
                                f"PnL будет рассчитан некорректно (entry_price=0). "
                                f"Рекомендуется проверить историю ордеров."
                            )
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

            # Атомарно обновляем позицию под блокировкой
            with self._position_lock:
                self._position = new_position
                self._position_qty = new_qty
                self._entry_price = new_entry_price
                self._last_price = new_last_price
        except Exception as e:
            logger.warning(f"[LiveEngine:{self._strategy_id}] "
                           f"Не удалось определить позицию: {e}")
            with self._position_lock:
                self._position = 0

    def _get_entry_price_from_history(self) -> float:
        """Берёт цену входа из последней незакрытой пары ордеров."""
        try:
            pairs = get_order_pairs(self._strategy_id)
            # Ищем незакрытую позицию (close=None)
            for pair in reversed(pairs):
                if pair["close"] is None and pair["open"]:
                    return float(pair["open"].get("price", 0))
        except Exception:
            pass
        return 0.0

    def _get_lookback(self) -> int:
        if hasattr(self._module, "get_lookback"):
            try:
                return int(self._module.get_lookback(self._params))
            except Exception:
                pass
        return 300

    def _poll_loop(self):
        """Основной цикл: загрузка истории → поллинг новых баров."""
        # Первая загрузка истории
        self._load_and_update()
        # Определяем позицию после подключения коннектора (данные уже пришли)
        if not self._entry_price:
            self._detect_position()

        while not self._stop_event.is_set():
            self._stop_event.wait(self._poll_interval)
            if self._stop_event.is_set():
                break
            if not self._connector.is_connected():
                continue
            try:
                self._load_and_update()
            except Exception as e:
                logger.error(f"[LiveEngine:{self._strategy_id}] Ошибка в poll_loop: {e}\n"
                             f"{traceback.format_exc()}")

    def _load_and_update(self):
        """Загружает историю, ищет новые бары, вызывает on_bar при необходимости.
        
        NOTE: Добавлен timeout и проверка stop_event для избежания блокировки
        потока при зависании get_history.
        """
        lookback = self._get_lookback()
        days = max(lookback // 50, 5)

        # Проверяем флаг остановки перед блокирующим вызовом
        if self._stop_event.is_set():
            return

        # Используем thread для get_history с timeout, чтобы не блокировать poll_loop
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
        
        # Ждём с таймаутом (30 секунд для QUIK, 10 для Финам)
        connector_id = getattr(self._connector, '_connector_id', 'finam')
        timeout = 30 if connector_id == 'quik' else 10
        fetch_thread.join(timeout=timeout)
        
        if fetch_thread.is_alive():
            # Таймаут - не блокируем poll_loop, записываем в лог
            logger.warning(
                f"[LiveEngine:{self._strategy_id}] get_history завис (>{timeout} сек), "
                f"пропускаем тик"
            )
            return
        
        if result['error']:
            logger.error(
                f"[LiveEngine:{self._strategy_id}] Ошибка get_history: {result['error']}"
            )
            return
            
        df = result['df']
        if df is None or df.empty:
            return

        # Конвертируем DataFrame в список баров
        bars = []
        for dt_idx, row in df.iterrows():
            dt = dt_idx.to_pydatetime() if hasattr(dt_idx, 'to_pydatetime') else dt_idx
            bars.append(_bar_from_row(row, dt))

        if not bars:
            return

        # Обновляем последнюю цену при каждом поллинге
        self._last_price = bars[-1]["close"]

        # Обновляем point_cost если ещё не загружен
        if self._point_cost == 1.0:
            self._load_point_cost()

        # Записываем equity для трекинга просадки
        self._record_equity()

        new_bar_dt = bars[-1]["dt"]

        if self._last_bar_dt is None:
            # Первая загрузка — сохраняем историю, не вызываем on_bar
            with self._bars_lock:
                self._bars = bars
                self._last_bar_dt = new_bar_dt
            logger.info(f"[LiveEngine:{self._strategy_id}] Загружено {len(bars)} баров, "
                        f"последний: {new_bar_dt}")
            return

        if new_bar_dt <= self._last_bar_dt:
            # Нет новых баров
            return

        # Есть новый бар!
        with self._bars_lock:
            self._bars = bars
            self._last_bar_dt = new_bar_dt

        logger.debug(f"[LiveEngine:{self._strategy_id}] Новый бар: {new_bar_dt} "
                      f"O={bars[-1]['open']} H={bars[-1]['high']} "
                      f"L={bars[-1]['low']} C={bars[-1]['close']}")
        self._process_bar()

    def _process_bar(self):
        """Пересчитывает индикаторы, вызывает on_bar(), исполняет сигнал.

        Важно: bars[-1] в списке, который приходит из get_history, — это текущий
        НЕЗАКРЫТЫЙ бар (TransAQ всегда включает его в ответ). Стратегия должна
        получать только закрытые бары, поэтому передаём bars[:-1].
        Именно этот подход соответствует логике TsLab: сигнал формируется на
        последнем ЗАКРЫТОМ баре, а исполнение происходит на открытии следующего.
        """
        with self._bars_lock:
            bars = list(self._bars)

        # Отбрасываем последний незакрытый бар: on_bar должен видеть только закрытые бары
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

        # Читаем позицию под блокировкой для защиты от race condition
        with self._position_lock:
            current_position = self._position

        signal = self._loaded.call_on_bar(processed_bars, current_position, self._params)

        action = signal.get("action") if signal else None
        if action:
            # Защита от двойного входа: проверяем позицию перед обработкой любого сигнала
            if action in ("buy", "sell"):
                with self._position_lock:
                    if self._position != 0:
                        logger.warning(
                            f"[LiveEngine:{self._strategy_id}] Позиция уже открыта "
                            f"({self._position}, qty={self._position_qty}), "
                            f"игнорируем {action.upper()}"
                        )
                        return

            logger.info(f"[LiveEngine:{self._strategy_id}] Сигнал: {signal}")
            # Если стратегия реализует execute_signal — делегируем ей (мультиинструментальные)
            if hasattr(self._module, "execute_signal"):
                try:
                    self._params["_strategy_id"] = self._strategy_id      # для записи сделок в стратегии
                    self._params["_connector_id"] = self._connector_id     # для расчёта комиссии
                    self._module.execute_signal(
                        signal, self._connector, self._params, self._account_id
                    )
                except Exception as e:
                    logger.error(f"[LiveEngine:{self._strategy_id}] "
                                 f"execute_signal error: {e}\n{traceback.format_exc()}")
            else:
                self._execute_signal(signal)

    def _calc_dynamic_qty(self, side: str) -> Optional[int]:
        """Рассчитывает динамический лот.

        Формула: Floor((free_money / (drawdown + GO)) / instances)

        Для фьючерсов: GO = buy_deposit / sell_deposit (гарантийное обеспечение)
        Для акций: GO = price * lot_size (стоимость позиции, т.к. ГО = 0)

        drawdown: max(ручная, по стратегии)
        """
        free_money = self._connector.get_free_money(self._account_id)
        if free_money is None or free_money <= 0:
            return None

        # Информация об инструменте
        sec_info = None
        if hasattr(self._connector, "get_sec_info"):
            sec_info = self._connector.get_sec_info(self._ticker, self._board)

        # ГО для фьючерсов (для акций = 0)
        go = 0.0
        if sec_info:
            go = float(sec_info.get("buy_deposit" if side == "buy" else "sell_deposit") or 0)

        # Просадка: max(ручная, по стратегии)
        manual_dd = float(self._lot_sizing.get("drawdown", 0))
        strat_dd = get_max_drawdown(self._strategy_id) or 0
        effective_dd = max(manual_dd, strat_dd)

        instances = max(int(self._lot_sizing.get("instances", 1)), 1)

        # Для АКЦИЙ (go=0): используем price * lot_size
        # NOTE: Исправлена формула. Для акций drawdown не имеет смысла как для фьючерсов,
        # так как ГО=0. Используем упрощённую формулу: максимальное количество лотов
        # которое можно купить на free_money.
        if go <= 0:
            price = self._last_price
            if price <= 0:
                # Fallback: статический лот
                return int(self._lot_sizing.get("lot", 1)) or 1

            lot_size = int(sec_info.get("lotsize", 1)) if sec_info else 1
            # Стоимость одного лота (позиции)
            position_cost = price * lot_size

            if position_cost <= 0:
                return int(self._lot_sizing.get("lot", 1)) or 1

            # Для акций: формула floor(free / (price * lot))
            # Это максимальное количество лотов которое можно купить
            qty = math.floor(free_money / position_cost / instances)
            # qty >= 1: можем купить хотя бы 1 лот → возвращаем qty
            # qty < 1: денег не хватает даже на 1 лот → возвращаем None
            return qty if qty >= 1 else None

        # Для ФЬЮЧЕРСОВ: используем ГО
        denom = effective_dd + go
        if denom <= 0:
            # Fallback: статический лот
            return int(self._lot_sizing.get("lot", 1)) or 1

        qty = math.floor((free_money / denom) / instances)
        # qty >= 1: можем купить хотя бы 1 контракт → возвращаем qty
        # qty < 1: денег не хватает даже на 1 контракт → fallback на статический лот
        return qty if qty >= 1 else int(self._lot_sizing.get("lot", 1)) or 1

    def _execute_signal(self, signal: dict):
        """Исполняет торговый сигнал через коннектор.

        order_mode='market'      — рыночная заявка.
        order_mode='limit'       — лимитка по лучшей цене в стакане (ChaseOrder).
        order_mode='limit_price' — лимитка по цене из сигнала (signal["price"]).
        """
        action = signal.get("action")
        qty = signal.get("qty", 1)
        
        # Валидация qty
        try:
            qty = int(qty)
            if qty <= 0:
                logger.error(f"[{self._strategy_id}] Некорректный qty={qty} в сигнале, должен быть > 0")
                self._record_failure()
                return
        except (TypeError, ValueError):
            logger.error(f"[{self._strategy_id}] Некорректный тип qty={qty} в сигнале, ожидается число")
            self._record_failure()
            return
        
        comment = signal.get("comment", "")

        # Динамический лот
        if action in ("buy", "sell") and self._lot_sizing.get("dynamic"):
            dyn_qty = self._calc_dynamic_qty(action)
            if dyn_qty is not None:
                qty = dyn_qty
                logger.info(f"[LiveEngine:{self._strategy_id}] Динамический лот: {qty}")
            else:
                logger.error(f"[{self._strategy_id}] Недостаточно средств для {action}, сигнал отклонён")
                self._record_failure()
                return

        fill_price = self._last_price

        try:
            if action in ("buy", "sell"):
                # Атомарная проверка позиции и флага in-flight под одной блокировкой
                # для предотвращения race condition при одновременных сигналах.
                # Используем единую блокировку _position_lock для избежания deadlock
                # (ранее использовались вложенные блокировки _position_lock + _order_in_flight_lock)
                with self._position_lock:
                    if self._position != 0:
                        logger.warning(
                            f"[LiveEngine:{self._strategy_id}] Позиция уже открыта "
                            f"({self._position}, qty={self._position_qty}), "
                            f"игнорируем {action.upper()}"
                        )
                        return

                    if self._order_mode in ("limit", "limit_price"):
                        if self._order_in_flight:
                            logger.warning(
                                f"[LiveEngine:{self._strategy_id}] Лимитный ордер уже в работе, "
                                f"игнорируем {action.upper()}"
                            )
                            return
                        self._order_in_flight = True

                if self._order_mode == "limit":
                    self._execute_chase(action, qty, comment)
                elif self._order_mode == "limit_price":
                    price = float(signal.get("price", 0)) or fill_price
                    self._execute_limit_price(action, qty, comment, price)
                else:
                    self._execute_market(action, qty, comment, fill_price)

            elif action == "close":
                with self._position_lock:
                    if self._position == 0 or self._position_qty == 0:
                        logger.warning(
                            f"[LiveEngine:{self._strategy_id}] Нет открытой позиции, игнорируем CLOSE"
                        )
                        return

                    if self._order_mode in ("limit", "limit_price"):
                        if self._order_in_flight:
                            logger.warning(
                                f"[LiveEngine:{self._strategy_id}] Лимитный ордер уже в работе, "
                                f"игнорируем CLOSE"
                            )
                            return
                        self._order_in_flight = True

                    close_side = "sell" if self._position == 1 else "buy"
                    close_qty = abs(self._position_qty)

                if self._order_mode == "limit":
                    self._execute_chase(close_side, close_qty, comment, is_close=True)
                elif self._order_mode == "limit_price":
                    price = float(signal.get("price", 0)) or fill_price
                    self._execute_limit_price(close_side, close_qty, comment, price, is_close=True)
                else:
                    self._execute_market_close(close_side, close_qty, comment, fill_price)

            # ORDER_PLACED только для не-chase режимов — для chase уведомление после исполнения
            if self._order_mode != "limit":
                try:
                    notifier.send(
                        EventCode.ORDER_PLACED,
                        agent=self._strategy_id,
                        description=f"{action.upper()} {self._ticker} x{qty} "
                                    f"[{self._order_mode}] | {comment}",
                    )
                except Exception:
                    pass

        except Exception as e:
            if self._order_mode in ("limit", "limit_price"):
                with self._position_lock:
                    self._order_in_flight = False
            logger.error(f"[LiveEngine:{self._strategy_id}] "
                         f"Ошибка исполнения {action}: {e}\n{traceback.format_exc()}")

    def _record_trade(self, side: str, qty: int, price: float, comment: str, order_type: str = "market"):
        """Записывает исполненную сделку в order_history и trades_history.

        Использует новый метод _calculate_commission() для расчета комиссии.
        Комиссия учитывается в order_history для расчёта net PnL через get_order_pairs().
        """
        # Рассчитываем комиссию через новый метод (автоопределение типа инструмента).
        # Храним и legacy-значение руб/лот, и точную абсолютную комиссию за сторону.
        commission_rub = self._calculate_commission(self._ticker, qty, price)
        commission_per_lot = commission_rub / abs(qty) if qty != 0 else 0
        
        logger.info(
            f"[LiveEngine:{self._strategy_id}] Запись сделки: {side.upper()} {self._ticker} "
            f"x{qty} @ {price:.4f}, комиссия={commission_rub:.2f} руб "
            f"({commission_per_lot:.2f} руб/лот)"
        )
        
        try:
            order = make_order(
                strategy_id=self._strategy_id,
                ticker=self._ticker,
                side=side,
                quantity=qty,
                price=price,
                board=self._board,
                comment=comment,
                commission=commission_per_lot,
                commission_total=commission_rub,
                point_cost=self._point_cost,
            )
            save_order(order)
        except Exception as e:
            logger.warning(f"[LiveEngine:{self._strategy_id}] _record_trade (order_history) error: {e}")

        try:
            trade = {
                "strategy_id": self._strategy_id,
                "agent_name": self._agent_name,
                "ticker": self._ticker,
                "board": self._board,
                "side": side,
                "qty": qty,
                "price": price,
                "commission": commission_rub,
                "order_type": order_type,
                "comment": comment,
                "dt": datetime.now().isoformat(),
            }
            append_trade(trade)
        except Exception as e:
            logger.warning(f"[LiveEngine:{self._strategy_id}] _record_trade (storage) error: {e}")

        # Принудительный flush equity после каждой сделки
        try:
            realized = get_total_pnl(self._strategy_id) or 0.0
            unrealized = 0.0
            
            if self._position_qty and self._last_price and self._entry_price:
                # Gross unrealized PnL
                gross_unrealized = (self._last_price - self._entry_price) * self._position_qty * self._point_cost
                
                # Комиссии за вход и выход
                entry_commission = self._calculate_commission(self._ticker, self._position_qty, self._entry_price)
                exit_commission = self._calculate_commission(self._ticker, self._position_qty, self._last_price)
                
                # Net unrealized PnL
                unrealized = gross_unrealized - entry_commission - exit_commission
            
            record_equity(self._strategy_id, realized + unrealized, self._position_qty or 0, force_flush=True)
        except Exception as e:
            logger.warning(f"[LiveEngine:{self._strategy_id}] equity flush error: {e}")

    def _execute_market(self, side: str, qty: int, comment: str, fill_price: float):
        """Рыночная заявка на открытие позиции.

        Позиция обновляется только после подтверждения исполнения ордера
        через мониторинг в фоновом потоке.
        """
        tid = self._connector.place_order(
            account_id=self._account_id,
            ticker=self._ticker,
            side=side,
            quantity=qty,
            order_type="market",
            board=self._board,
            agent_name=self._agent_name,
        )
        if tid:
            self._record_success()
            logger.info(f"[LiveEngine:{self._strategy_id}] "
                        f"MARKET {side.upper()} x{qty} tid={tid} (мониторинг...)")

            # Запускаем фоновый мониторинг заявки
            t = threading.Thread(
                target=self._monitor_market_order,
                args=(tid, side, qty, fill_price, comment, False),
                daemon=True,
                name=f"market-monitor-{self._strategy_id}-{tid}",
            )
            t.start()
        else:
            self._record_failure()
            logger.error(
                f"[LiveEngine:{self._strategy_id}] ОШИБКА заявки: "
                f"агент={self._strategy_id} тикер={self._ticker} "
                f"сторона={side.upper()} qty={qty} цена={fill_price} "
                f"вид=market | {comment}"
            )

    def _execute_market_close(self, close_side: str, close_qty: int, comment: str, fill_price: float):
        """Рыночное закрытие позиции.

        Использует close_position() если доступен, иначе place_order market.
        Позиция обновляется только после подтверждения исполнения ордера.
        """
        # Пробуем close_position (коннектор сам определяет qty из позиции)
        # close_position может вернуть tid для мониторинга или True/False
        tid_or_ok = False
        use_close_position = hasattr(self._connector, "close_position")

        if use_close_position:
            try:
                tid_or_ok = self._connector.close_position(
                    account_id=self._account_id,
                    ticker=self._ticker,
                    agent_name=self._agent_name,
                )
            except Exception as e:
                logger.warning(f"[LiveEngine:{self._strategy_id}] close_position error: {e}")
                tid_or_ok = False

        # Fallback: рыночный ордер напрямую
        if not tid_or_ok:
            tid = self._connector.place_order(
                account_id=self._account_id,
                ticker=self._ticker,
                side=close_side,
                quantity=close_qty,
                order_type="market",
                board=self._board,
                agent_name=self._agent_name,
            )
            if tid:
                tid_or_ok = tid
                self._record_success()
                logger.info(f"[LiveEngine:{self._strategy_id}] "
                            f"CLOSE MARKET {close_side.upper()} x{close_qty} tid={tid} (мониторинг...)")

                # Запускаем фоновый мониторинг заявки
                t = threading.Thread(
                    target=self._monitor_market_order,
                    args=(tid, close_side, close_qty, fill_price, comment, True),
                    daemon=True,
                    name=f"market-close-monitor-{self._strategy_id}-{tid}",
                )
                t.start()
                return  # Мониторинг обновит позицию в фоне
            else:
                self._record_failure()
                logger.error(
                    f"[LiveEngine:{self._strategy_id}] ОШИБКА заявки: "
                    f"агент={self._strategy_id} тикер={self._ticker} "
                    f"сторона={close_side.upper()} qty={close_qty} цена={fill_price} "
                    f"вид=market(close) | {comment}"
                )
                return

        # close_position вернул True (без tid) - считаем исполненным
        if tid_or_ok is True:
            with self._position_lock:
                self._record_trade(close_side, close_qty, fill_price, comment, order_type="market")
                self._position = 0
                self._position_qty = 0
                self._entry_price = 0.0
            logger.info(f"[LiveEngine:{self._strategy_id}] CLOSE ({comment})")
        elif tid_or_ok:  # это tid - запускаем мониторинг
            t = threading.Thread(
                target=self._monitor_market_order,
                args=(tid_or_ok, close_side, close_qty, fill_price, comment, True),
                daemon=True,
                name=f"market-close-monitor-{self._strategy_id}-{tid_or_ok}",
            )
            t.start()
        else:
            logger.error(
                f"[LiveEngine:{self._strategy_id}] ОШИБКА закрытия: "
                f"агент={self._strategy_id} тикер={self._ticker} "
                f"сторона={close_side.upper()} qty={close_qty} цена={fill_price} "
                f"вид=market(close) | {comment}"
            )

    def _execute_limit_price(self, side: str, qty: int, comment: str, price: float, is_close: bool = False):
        """Лимитная заявка по фиксированной цене из сигнала.

        Заявка выставляется и мониторится в фоновом потоке до полного исполнения
        или до 23:45 (1425 мин), после чего снимается.

        Позиция НЕ обновляется сразу — только после фактического исполнения.
        """
        tid = self._connector.place_order(
            account_id=self._account_id,
            ticker=self._ticker,
            side=side,
            quantity=qty,
            order_type="limit",
            price=price,
            board=self._board,
            agent_name=self._agent_name,
        )
        if not tid:
            logger.error(
                f"[LiveEngine:{self._strategy_id}] ОШИБКА заявки: "
                f"агент={self._strategy_id} тикер={self._ticker} "
                f"сторона={side.upper()} qty={qty} цена={price} "
                f"вид=limit_price | {comment}"
            )
            self._record_failure()
            with self._position_lock:
                self._order_in_flight = False
            return

        logger.info(f"[LiveEngine:{self._strategy_id}] "
                    f"LIMIT {side.upper()} x{qty} @ {price} tid={tid} ({comment})")

        # Запускаем фоновый мониторинг заявки
        t = threading.Thread(
            target=self._monitor_limit_price_order,
            args=(tid, side, qty, price, comment, is_close),
            daemon=True,
            name=f"limit-monitor-{self._strategy_id}-{tid}",
        )
        t.start()

    def _monitor_limit_price_order(self, tid: str, side: str, qty: int,
                                   price: float, comment: str, is_close: bool):
        """
        Фоновый мониторинг лимитной заявки по фиксированной цене.

        Ждёт исполнения заявки. В 23:45 (1425 мин) снимает заявку если не исполнена.
        После исполнения обновляет позицию и записывает сделку.

        Учитывает частичное исполнение: filled = qty - balance.
        """
        _TERMINAL = {"matched", "cancelled", "canceled", "denied", "removed", "expired", "killed"}
        CANCEL_TIME_MIN = TRADING_END_TIME_MIN

        filled = 0
        cancelled_by_time = False

        logger.debug(f"[LiveEngine:{self._strategy_id}] "
                     f"Мониторинг LIMIT tid={tid} {side.upper()} x{qty} @ {price}")

        while self._running:
            try:
                info = self._connector.get_order_status(tid)
            except Exception as e:
                logger.warning(f"[LiveEngine:{self._strategy_id}] "
                               f"get_order_status tid={tid}: {e}")
                info = None

            if info:
                status = info.get("status", "")
                balance = info.get("balance")
                quantity_field = info.get("quantity")

                # Считаем фактически исполненный объём
                if balance is not None and quantity_field is not None:
                    filled = int(quantity_field) - int(balance)

                if status in _TERMINAL:
                    logger.info(f"[LiveEngine:{self._strategy_id}] "
                                f"LIMIT tid={tid} статус={status} filled={filled}/{qty}")
                    break

            # Проверяем время — снимаем в 23:45
            now_min = datetime.now().hour * 60 + datetime.now().minute
            if now_min >= CANCEL_TIME_MIN:
                logger.info(f"[LiveEngine:{self._strategy_id}] "
                            f"LIMIT tid={tid} снимается по времени 23:45 (filled={filled}/{qty})")
                try:
                    self._connector.cancel_order(tid, self._account_id)
                except Exception as e:
                    logger.warning(f"[LiveEngine:{self._strategy_id}] "
                                   f"cancel_order tid={tid}: {e}")
                cancelled_by_time = True
                # Ждём финального статуса после отмены
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    time.sleep(0.1)
                    try:
                        info2 = self._connector.get_order_status(tid)
                        if info2 and info2.get("status", "") in _TERMINAL:
                            b2 = info2.get("balance")
                            q2 = info2.get("quantity")
                            if b2 is not None and q2 is not None:
                                filled = int(q2) - int(b2)
                            break
                    except Exception:
                        pass
                break

            time.sleep(1.0)

        # Обновляем позицию по фактически исполненному объёму и освобождаем in-flight флаг
        with self._position_lock:
            self._order_in_flight = False
            if filled > 0:
                if is_close:
                    if filled >= qty:
                        self._position = 0
                        self._position_qty = 0
                        self._entry_price = 0.0
                    else:
                        remaining = abs(self._position_qty) - filled
                        self._position_qty = remaining if self._position == 1 else -remaining
                        if remaining == 0:
                            self._position = 0
                            self._entry_price = 0.0
                else:
                    if side == "buy":
                        self._position = 1
                        self._position_qty = filled
                    else:
                        self._position = -1
                        self._position_qty = -filled
                    self._entry_price = price

                self._record_trade(side, filled, price, comment, order_type="limit")
                logger.info(f"[LiveEngine:{self._strategy_id}] "
                            f"LIMIT исполнена: {side.upper()} filled={filled}/{qty} @ {price} "
                            f"{'(снята по времени, частично)' if cancelled_by_time and filled < qty else ''}")

                # Уведомление об исполнении
                try:
                    notifier.send(
                        EventCode.ORDER_FILLED,
                        agent=self._strategy_id,
                        description=f"{side.upper()} {self._ticker} x{filled} @ {price} "
                                    f"[limit_price] | {comment}",
                    )
                except Exception:
                    pass
            else:
                if cancelled_by_time:
                    logger.info(f"[LiveEngine:{self._strategy_id}] "
                                f"LIMIT tid={tid} снята в 23:45, не исполнена")
                else:
                    logger.warning(f"[LiveEngine:{self._strategy_id}] "
                                   f"LIMIT tid={tid} завершена без исполнения")

    def _monitor_market_order(self, tid: str, side: str, qty: int,
                              price: float, comment: str, is_close: bool) -> bool:
        """
        Мониторинг рыночного ордера до подтверждения исполнения.

        Ожидает статус 'matched' или терминальный статус с таймаутом.
        Возвращает True если ордер исполнен (полностью или частично),
        False если не исполнен или отклонён.

        Позиция обновляется только после подтверждения исполнения.
        """
        _TERMINAL = {"matched", "cancelled", "canceled", "denied", "removed", "expired", "killed"}
        TIMEOUT_SEC = 30  # Таймаут ожидания исполнения market-ордера

        filled = 0
        confirmed = False

        logger.debug(f"[LiveEngine:{self._strategy_id}] "
                     f"Мониторинг MARKET tid={tid} {side.upper()} x{qty}")

        deadline = time.monotonic() + TIMEOUT_SEC

        while self._running and time.monotonic() < deadline:
            try:
                info = self._connector.get_order_status(tid)
            except Exception as e:
                logger.warning(f"[LiveEngine:{self._strategy_id}] "
                               f"get_order_status tid={tid}: {e}")
                info = None

            if info:
                status = info.get("status", "")
                balance = info.get("balance")
                quantity_field = info.get("quantity")

                # Считаем фактически исполненный объём
                if balance is not None and quantity_field is not None:
                    filled = int(quantity_field) - int(balance)

                if status == "matched":
                    confirmed = True
                    logger.info(f"[LiveEngine:{self._strategy_id}] "
                                f"MARKET tid={tid} исполнен filled={filled}/{qty}")
                    break
                elif status in _TERMINAL:
                    logger.info(f"[LiveEngine:{self._strategy_id}] "
                                f"MARKET tid={tid} завершён статус={status} filled={filled}/{qty}")
                    break

            time.sleep(0.5)

        # Обновляем позицию по фактически исполненному объёму (под блокировкой)
        with self._position_lock:
            if filled > 0 and confirmed:
                if is_close:
                    if filled >= qty:
                        self._position = 0
                        self._position_qty = 0
                        self._entry_price = 0.0
                    else:
                        remaining = abs(self._position_qty) - filled
                        self._position_qty = remaining if self._position == 1 else -remaining
                        if remaining == 0:
                            self._position = 0
                            self._entry_price = 0.0
                else:
                    if side == "buy":
                        self._position = 1
                        self._position_qty = filled
                    else:
                        self._position = -1
                        self._position_qty = -filled
                    self._entry_price = price

                self._record_trade(side, filled, price, comment, order_type="market")
                logger.info(f"[LiveEngine:{self._strategy_id}] "
                            f"MARKET подтверждено: {side.upper()} filled={filled}/{qty} @ {price}")

                # Уведомление об исполнении
                try:
                    notifier.send(
                        EventCode.ORDER_FILLED,
                        agent=self._strategy_id,
                        description=f"{side.upper()} {self._ticker} x{filled} @ {price} "
                                    f"[market] | {comment}",
                    )
                except Exception:
                    pass

                return True
            else:
                if filled > 0:
                    # Частичное исполнение без статуса matched - тоже учитываем
                    if is_close:
                        if filled >= qty:
                            self._position = 0
                            self._position_qty = 0
                            self._entry_price = 0.0
                        else:
                            remaining = abs(self._position_qty) - filled
                            self._position_qty = remaining if self._position == 1 else -remaining
                    else:
                        if side == "buy":
                            self._position = 1
                            self._position_qty = filled
                        else:
                            self._position = -1
                            self._position_qty = -filled
                        self._entry_price = price
                    self._record_trade(side, filled, price, comment, order_type="market")
                    logger.info(f"[LiveEngine:{self._strategy_id}] "
                                f"MARKET частично: {side.upper()} filled={filled}/{qty}")
                    return True
                logger.warning(f"[LiveEngine:{self._strategy_id}] "
                               f"MARKET tid={tid} не исполнен за {TIMEOUT_SEC} сек")
                return False

    def _execute_chase(self, side: str, qty: int, comment: str, is_close: bool = False):
        """Лимитная заявка через ChaseOrder (стакан).

        Запускается в отдельном daemon-потоке — НЕ блокирует poll_loop.
        Позиция обновляется по завершению через _on_chase_done().
        Защита от двойного входа: _order_in_flight флаг (уже установлен в _execute_signal).
        """
        # Флаг _order_in_flight уже установлен в _execute_signal() под блокировкой
        # Здесь только запускаем chase-поток

        logger.info(f"[LiveEngine:{self._strategy_id}] "
                    f"Chase {side.upper()} x{qty} ({comment}) — фоновый поток")

        chase = ChaseOrder(
            connector=self._connector,
            account_id=self._account_id,
            ticker=self._ticker,
            side=side,
            quantity=qty,
            board=self._board,
            agent_name=self._agent_name,
        )

        with self._chase_lock:
            self._active_chase_orders.append(chase)

        def _run():
            try:
                chase.wait(timeout=120)
                
                # Проверяем процент исполнения после завершения chase
                filled_qty = chase.filled_qty
                target_qty = qty
                fill_rate = (filled_qty / target_qty * 100) if target_qty > 0 else 0
                
                if fill_rate < 50:
                    logger.warning(
                        f"[{self._strategy_id}] Частичное исполнение: "
                        f"{filled_qty}/{target_qty} ({fill_rate:.1f}%)"
                    )
                    # TODO: Опционально можно добавить retry-логику здесь
                elif fill_rate < 100:
                    logger.info(
                        f"[{self._strategy_id}] Неполное исполнение: "
                        f"{filled_qty}/{target_qty} ({fill_rate:.1f}%)"
                    )
            finally:
                with self._chase_lock:
                    if chase in self._active_chase_orders:
                        self._active_chase_orders.remove(chase)

                if not chase.is_done:
                    chase.cancel()

                self._on_chase_done(chase, side, qty, comment, is_close)

        t = threading.Thread(
            target=_run,
            daemon=True,
            name=f"chase-{self._strategy_id}-{side}",
        )
        t.start()

    def _on_chase_done(self, chase, side: str, qty: int,
                       comment: str, is_close: bool):
        """Вызывается из фонового потока после завершения ChaseOrder.
        Обновляет позицию и записывает сделку под _position_lock.
        """
        filled = chase.filled_qty
        avg_px = chase.avg_price

        if filled <= 0:
            logger.error(
                f"[LiveEngine:{self._strategy_id}] ОШИБКА заявки: "
                f"агент={self._strategy_id} тикер={self._ticker} "
                f"сторона={side.upper()} qty={qty} цена=bid/offer "
                f"вид=limit(стакан) — ничего не исполнено за 60 сек | {comment}"
            )
            self._record_failure()
            # Сбрасываем флаг чтобы не блокировать будущие ордера
            with self._position_lock:
                self._order_in_flight = False
            return

        with self._position_lock:
            if is_close:
                if filled >= qty:
                    self._position = 0
                    self._position_qty = 0
                    self._entry_price = 0.0
                else:
                    remaining = abs(self._position_qty) - filled
                    self._position_qty = (
                        remaining if self._position == 1 else -remaining
                    )
                    if remaining == 0:
                        self._position = 0
                        self._entry_price = 0.0
            else:
                self._position = 1 if side == "buy" else -1
                self._position_qty = filled if side == "buy" else -filled
                self._entry_price = avg_px

            # Сбрасываем флаг под той же блокировкой, под которой он устанавливался
            self._order_in_flight = False

        self._record_trade(side, filled, avg_px, comment, order_type="chase")
        self._record_success()

        logger.info(
            f"[LiveEngine:{self._strategy_id}] "
            f"Chase done: {side.upper()} filled={filled}/{qty} "
            f"avg={avg_px:.4f} ({comment})"
        )

        try:
            notifier.send(
                EventCode.ORDER_FILLED,
                agent=self._strategy_id,
                description=f"{side.upper()} {self._ticker} x{filled} @ {avg_px:.4f} "
                            f"[chase] | {comment}",
            )
        except Exception:
            pass

    def __repr__(self):
        return (f"<LiveEngine {self._strategy_id} ticker={self._ticker} "
                f"pos={self._position} running={self._running}>")
