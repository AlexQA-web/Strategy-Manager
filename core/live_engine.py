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
import weakref
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
from typing import Optional

import pandas as pd
from loguru import logger

from core.equity_tracker import flush_all as equity_flush_all, record_equity, get_max_drawdown
from config.settings import COMMISSION_FUTURES, COMMISSION_STOCK, TRADING_END_TIME_MIN, DEFAULT_STRATEGY_LOOKBACK
from core.commission_manager import commission_manager
from core.valuation_service import valuation_service
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
from core.startup_service import fetch_startup_snapshot
from core.strategy_runtime import StrategyRuntimeState, is_valid_runtime_transition
from core.base_connector import MarketDataEnvelope

# Маппинг timeframe → строка для get_history
TIMEFRAME_TO_PERIOD = {
    "1": "1m", "5": "5m", "15": "15m", "30": "30m", "60": "1h",
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h",
}

PERIOD_TO_BAR_MINUTES = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "1d": 1440,
}

_TRADING_SESSION_MINUTES = 8 * 60
_WARMUP_HISTORY_SAFETY_FACTOR = 1.25
_MIN_WARMUP_HISTORY_DAYS = 5
_MAX_WARMUP_HISTORY_DAYS = 3650
_MAX_WARMUP_FETCH_ATTEMPTS = 3
_INCREMENTAL_HISTORY_DAYS = 5

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
                 lot_sizing: dict = None, allow_shared_position: bool = False):
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
        self._allow_shared_position = allow_shared_position

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
        self._subscribed_reconnect = False

        # Счётчик тайм-аутов get_history
        self._consecutive_timeouts: int = 0
        self._MAX_CONSECUTIVE_TIMEOUTS = 5

        # Статус синхронизации с брокером: "unknown" | "synced" | "stale"
        # При stale запрещены открывающие сделки, разрешены только close/reconcile
        self._sync_status: str = "unknown"
        self._runtime_state: str = StrategyRuntimeState.STOPPED.value
        self._startup_snapshot_data: dict = {}

        # Bounded executor для get_history (max 1, чтобы не плодить потоки при timeout)
        self._history_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"history-{strategy_id}"
        )
        self._history_pool_closed = False
        self._history_pool_finalizer = weakref.finalize(
            self, LiveEngine._shutdown_executor, self._history_pool
        )

        # Параметры для point_cost и lot_size
        self._last_price: float = 0.0
        self._last_price_envelope: Optional[MarketDataEnvelope] = None
        self._point_cost: float = 1.0
        self._lot_size: int = 1

        # === Компоненты (делегирование бизнес-логики) ===

        # 1. PositionTracker
        self._position_tracker = PositionTracker()

        # 2. RiskGuard
        self._risk_guard = RiskGuard(
            strategy_id=self._strategy_id,
            circuit_breaker_threshold=int(params.get("circuit_breaker_threshold", 3) or 3),
            circuit_breaker_timeout=float(params.get("circuit_breaker_timeout", 60.0) or 60.0),
            max_position_size=int(params.get("max_position_size", 0) or 0),
            daily_loss_limit=float(params.get("daily_loss_limit", 0.0) or 0.0),
            get_total_pnl=get_total_pnl,
            get_current_equity=self._get_current_equity_metric,
            per_instrument_limits=params.get("per_instrument_limits", {}),
            max_trades_per_window=int(params.get("max_trades_per_window", 0) or 0),
            trade_window_sec=float(params.get("trade_window_sec", 60.0) or 60.0),
            cooldown_after_close_sec=float(params.get("cooldown_after_close_sec", 0.0) or 0.0),
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
            get_last_price_envelope=lambda: self._last_price_envelope,
            get_point_cost=lambda: self._point_cost,
            get_lot_size=lambda: self._lot_size,
            is_futures=lambda: self._is_futures(),
            calculate_commission=self._calculate_commission,
            on_reconcile=self._detect_position,
            on_circuit_break=self._on_circuit_break,
            on_manual_intervention=self._require_manual_intervention,
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
            on_broker_unavailable=self._on_broker_unavailable,
            on_history_divergence=self._require_manual_intervention,
            allow_shared_position=self._allow_shared_position,
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

    @property
    def sync_status(self) -> str:
        """Текущий статус синхронизации с брокером: unknown | synced | stale."""
        return self._sync_status

    @property
    def runtime_state(self) -> str:
        """Текущее runtime-состояние стратегии."""
        return self._runtime_state

    def _set_runtime_state(self, new_state: str):
        current_state = self._runtime_state
        if not is_valid_runtime_transition(current_state, new_state):
            logger.warning(
                f"[LiveEngine:{self._strategy_id}] runtime_state invalid transition: "
                f"{current_state} -> {new_state}"
            )
        if current_state != new_state:
            logger.info(
                f"[LiveEngine:{self._strategy_id}] runtime_state: "
                f"{current_state} -> {new_state}"
            )
        self._runtime_state = new_state

    def startup_preflight(self) -> bool:
        """Загружает startup snapshot и выполняет initial reconcile до запуска poll-loop."""
        self._set_runtime_state(StrategyRuntimeState.INITIALIZING.value)
        try:
            self._startup_snapshot_data = fetch_startup_snapshot(
                self._connector,
                self._account_id,
                self._strategy_id,
            )
            unresolved = self._startup_snapshot_data.get("pending_recovery", {}).get("unresolved", [])
            if unresolved:
                self._sync_status = "stale"
                self._require_manual_intervention(
                    f"unresolved_pending_orders:{len(unresolved)}"
                )
                return False
            self._detect_position()
            self._reconciler._last_reconcile_ts = 0.0
            self._reconciler.reconcile()

            if self._sync_status == "unknown":
                self._sync_status = "stale"

            if self._sync_status == "synced":
                self._set_runtime_state(StrategyRuntimeState.SYNCED.value)
            else:
                self._set_runtime_state(StrategyRuntimeState.DEGRADED.value)
            return True
        except Exception as e:
            logger.error(f"[LiveEngine:{self._strategy_id}] startup_preflight failed: {e}")
            self._sync_status = "stale"
            self._set_runtime_state(StrategyRuntimeState.FAILED_START.value)
            return False

    # === Вспомогательные методы ===

    def _is_futures(self, ticker: str = None, board: str = None) -> bool:
        t = ticker or self._ticker
        b = board if board is not None else (self._board if ticker is None else "")
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
        """Рассчитывает комиссию вручную.

        Для нефьючерсных инструментов trade_value считается с учётом lot_size,
        чтобы результат совпадал с auto-mode при эквивалентных тарифах.
        """
        if sec_type == 'futures':
            commission_per_lot = self._commission_rub if self._commission_rub > 0 else COMMISSION_FUTURES
            return commission_per_lot * abs_qty
        else:
            commission_pct = self._commission_pct if self._commission_pct > 0 else COMMISSION_STOCK
            lot_size = self._lot_size if self._lot_size > 0 else 1
            trade_value = price * abs_qty * lot_size
            return trade_value * (commission_pct / 100.0)

    def _get_pnl_multiplier(self) -> float:
        """Возвращает денежный множитель для расчёта PnL.

        Делегирует в ValuationService.get_pnl_multiplier.
        """
        return valuation_service.get_pnl_multiplier(
            is_futures=self._is_futures(),
            point_cost=self._point_cost,
            lot_size=self._lot_size,
        )

    @staticmethod
    def _shutdown_executor(executor: ThreadPoolExecutor):
        """Аккуратно останавливает executor даже если stop() не был вызван явно."""
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)
        except Exception:
            pass

    def _cleanup_history_pool(self):
        if self._history_pool_closed:
            return
        self._history_pool_closed = True
        self._shutdown_executor(self._history_pool)
        if hasattr(self, "_history_pool_finalizer") and self._history_pool_finalizer.alive:
            self._history_pool_finalizer.detach()

    def _get_current_equity_metric(self) -> Optional[float]:
        """Возвращает текущий equity = realized + unrealized для risk gate."""
        try:
            realized = get_total_pnl(self._strategy_id) or 0.0
            position_qty = self._position_tracker.get_position_qty()
            entry_price = self._position_tracker.get_entry_price()

            last_price = self._last_price
            if not last_price and hasattr(self._connector, "get_last_price"):
                try:
                    last_price = self._connector.get_last_price(self._ticker, self._board) or 0.0
                except Exception:
                    last_price = 0.0

            entry_commission = 0.0
            exit_commission = 0.0
            if position_qty and last_price and entry_price:
                entry_commission = self._calculate_commission(
                    self._ticker, position_qty, entry_price
                )
                exit_commission = self._calculate_commission(
                    self._ticker, position_qty, last_price
                )

            return valuation_service.compute_equity_snapshot(
                realized_pnl=realized,
                entry_price=entry_price or 0.0,
                current_price=last_price or 0.0,
                position_qty=position_qty or 0,
                pnl_multiplier=self._get_pnl_multiplier(),
                entry_commission=entry_commission,
                exit_commission=exit_commission,
            )
        except Exception:
            return None

    def _custom_pretrade_risk_check(self, action: str, qty: int) -> tuple[bool, str]:
        """Общий pre-trade risk gate для явно зарегистрированных custom adapters."""
        if action not in ("buy", "sell"):
            return True, ""
        if self._risk_guard.is_circuit_open():
            return False, "circuit breaker открыт"
        return self._risk_guard.check_risk_limits(action, qty, ticker=self._ticker)

    def _custom_account_risk_check(
        self,
        action: str,
        qty: int,
        ticker: str = "",
        board: str = "",
        last_price: float = 0.0,
    ) -> Optional[str]:
        return self._order_executor.check_account_risk_limits_for_order(
            action=action,
            qty=qty,
            ticker=ticker or self._ticker,
            board=board or self._board,
            last_price=last_price or 0.0,
        )

    def _custom_reserve_capital(
        self,
        action: str,
        qty: int,
        ticker: str = "",
        board: str = "",
        last_price: float = 0.0,
    ) -> str:
        return self._order_executor.reserve_capital_for_order(
            action=action,
            qty=qty,
            ticker=ticker or self._ticker,
            board=board or self._board,
            last_price=last_price or 0.0,
        )

    def _custom_release_capital(self, reservation_key: str):
        self._order_executor.release_reserved_capital(reservation_key)

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
        pnl_multiplier = self._get_pnl_multiplier()

        entry_commission = self._calculate_commission(self._ticker, qty, entry_price)
        exit_commission = self._calculate_commission(self._ticker, qty, price)
        net_pnl = valuation_service.compute_open_pnl(
            entry_price=entry_price,
            current_price=price,
            qty=qty,
            pnl_multiplier=pnl_multiplier,
            entry_commission=entry_commission,
            exit_commission=exit_commission,
        )

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
            position_qty = self._position_tracker.get_position_qty()
            last_price = self._last_price
            entry_price = self._position_tracker.get_entry_price()

            entry_commission = 0.0
            exit_commission = 0.0
            if position_qty and last_price and entry_price:
                entry_commission = self._calculate_commission(self._ticker, position_qty, entry_price)
                exit_commission = self._calculate_commission(self._ticker, position_qty, last_price)

            equity = valuation_service.compute_equity_snapshot(
                realized_pnl=realized,
                entry_price=entry_price or 0.0,
                current_price=last_price or 0.0,
                position_qty=position_qty or 0,
                pnl_multiplier=self._get_pnl_multiplier(),
                entry_commission=entry_commission,
                exit_commission=exit_commission,
            )
            record_equity(self._strategy_id, equity, position_qty or 0)
        except Exception as e:
            logger.debug(f"[LiveEngine:{self._strategy_id}] equity_tracker error: {e}")

    # === Start / Stop ===

    def start(self) -> bool:
        """Запускает daemon-поток поллинга.

        Returns:
            True если engine успешно запущен, False при ошибке.
        """
        if self._running:
            return True
        if self._runtime_state == StrategyRuntimeState.FAILED_START.value:
            return False
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
            self._set_runtime_state(StrategyRuntimeState.FAILED_START.value)
            return False

        if not point_cost_ok and not is_fut:
            logger.warning(
                f"[LiveEngine:{self._strategy_id}] "
                f"point_cost не загружен для {self._ticker}, используется 1.0"
            )
            self._point_cost = 1.0

        if hasattr(self._connector, "subscribe_reconnect"):
            self._connector.subscribe_reconnect(self._on_connector_reconnect)
            self._subscribed_reconnect = True
        else:
            self._connector.on_reconnect(self._on_connector_reconnect)

        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True,
                                        name=f"LiveEngine-{self._strategy_id}")
        self._thread.start()
        if self._sync_status == "synced":
            self._set_runtime_state(StrategyRuntimeState.TRADING.value)
        else:
            self._set_runtime_state(StrategyRuntimeState.DEGRADED.value)
        logger.info(f"[LiveEngine:{self._strategy_id}] Запущен, позиция={self._position_tracker.get_position()}")
        return True

    def stop(self):
        """Останавливает поток поллинга и выполняет graceful shutdown.

        SAFETY: stop() НИКОГДА не закрывает позицию автоматически.
        Открытая позиция сохраняется на бирже. Закрытие — только явное
        действие оператора через UI (manual flatten).
        """
        if not self._running:
            return

        self._set_runtime_state(StrategyRuntimeState.STOPPING.value)

        qty = self._position_tracker.get_position_qty()
        if qty != 0:
            logger.warning(
                f"[LiveEngine:{self._strategy_id}] "
                f"Остановка при открытой позиции (qty={qty}). "
                f"Позиция сохраняется на бирже — требуется ручное закрытие."
            )

        self._running = False
        self._stop_event.set()

        # Останавливаем компоненты
        self._order_executor.stop()
        self._cleanup_history_pool()

        # Ждём завершения основного потока
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

        if self._subscribed_reconnect and hasattr(self._connector, "unsubscribe_reconnect"):
            try:
                self._connector.unsubscribe_reconnect(self._on_connector_reconnect)
            except Exception as e:
                logger.warning(f"[LiveEngine:{self._strategy_id}] Ошибка отписки от reconnect: {e}")
            self._subscribed_reconnect = False

        self._unsubscribe_quotes()
        equity_flush_all()
        self._set_runtime_state(StrategyRuntimeState.STOPPED.value)

        logger.info(f"[LiveEngine:{self._strategy_id}] остановлен")

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

    def _get_last_bar_dt(self) -> Optional[datetime]:
        with self._bars_lock:
            return self._last_bar_dt

    def _update_bar_state(self, bars: list[dict], new_bar_dt: datetime) -> tuple[bool, bool]:
        """Атомарно сравнивает и обновляет bars state.

        Returns:
            (updated, was_initial)
        """
        with self._bars_lock:
            was_initial = self._last_bar_dt is None
            if self._last_bar_dt is not None and new_bar_dt <= self._last_bar_dt:
                return False, False
            self._bars = list(bars)
            self._last_bar_dt = new_bar_dt
            return True, was_initial

    # === Позиция и reconciliation ===

    def _detect_position(self):
        """Определяет текущую позицию по тикеру из коннектора."""
        try:
            if not self._allow_shared_position:
                try:
                    from core.autostart import has_strategy_collision

                    if has_strategy_collision(self._account_id, self._ticker, self._strategy_id):
                        self._sync_status = "stale"
                        self._require_manual_intervention("multi_strategy_collision")
                        return
                except Exception as collision_exc:
                    logger.debug(
                        f"[LiveEngine:{self._strategy_id}] collision guard check failed: {collision_exc}"
                    )
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
            self._order_executor.release_blocked_submissions_after_reconcile()
            self._last_price = new_last_price
            if self._sync_status != "synced":
                logger.info(
                    f"[LiveEngine:{self._strategy_id}] sync_status: "
                    f"{self._sync_status} → synced"
                )
            self._sync_status = "synced"
            if self._running:
                self._set_runtime_state(StrategyRuntimeState.TRADING.value)
            elif self._runtime_state in {
                StrategyRuntimeState.INITIALIZING.value,
                StrategyRuntimeState.DEGRADED.value,
                StrategyRuntimeState.STALE.value,
                StrategyRuntimeState.MANUAL_INTERVENTION_REQUIRED.value,
            }:
                self._set_runtime_state(StrategyRuntimeState.SYNCED.value)
        except Exception as e:
            prev = self._sync_status
            self._sync_status = "stale"
            if self._running:
                self._set_runtime_state(StrategyRuntimeState.DEGRADED.value)
            else:
                self._set_runtime_state(StrategyRuntimeState.STALE.value)
            logger.warning(f"[LiveEngine:{self._strategy_id}] "
                           f"Не удалось определить позицию: {e} — "
                           f"сохраняю последнюю подтверждённую: "
                           f"pos={self._position_tracker.get_position()} "
                           f"qty={self._position_tracker.get_position_qty()} "
                           f"sync_status: {prev} → stale")

    def _on_broker_unavailable(self):
        """Callback из Reconciler: данные брокера недоступны → degraded state."""
        if self._sync_status != "stale":
            logger.warning(
                f"[LiveEngine:{self._strategy_id}] sync_status: "
                f"{self._sync_status} → stale (broker unavailable в reconcile)"
            )
            self._sync_status = "stale"
        if self._running:
            self._set_runtime_state(StrategyRuntimeState.DEGRADED.value)
        else:
            self._set_runtime_state(StrategyRuntimeState.STALE.value)

    def _require_manual_intervention(self, reason: str):
        """Переводит стратегию в состояние ручного вмешательства."""
        if self._sync_status != "stale":
            logger.warning(
                f"[LiveEngine:{self._strategy_id}] sync_status: "
                f"{self._sync_status} → stale ({reason})"
            )
        self._sync_status = "stale"
        self._set_runtime_state(StrategyRuntimeState.MANUAL_INTERVENTION_REQUIRED.value)

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

    def manual_close_position(self):
        """Ручное закрытие позиции — вызывается ТОЛЬКО оператором через UI.

        SAFETY: Этот метод НЕ вызывается автоматически из stop(),
        timeout, circuit breaker или crash paths.
        Это единственная точка входа для экстренного закрытия.

        Returns:
            str: "success" | "no_position" | "order_in_flight" | "close_failed"
        """
        qty = self._position_tracker.get_position_qty()
        pos = self._position_tracker.get_position()

        if qty == 0 or pos == 0:
            return "no_position"

        # Проверяем, не ведётся ли уже закрытие
        if self._position_tracker.is_order_in_flight():
            logger.warning(
                f"[LiveEngine:{self._strategy_id}] "
                f"Ручное закрытие пропущено — ордер уже в работе"
            )
            return "order_in_flight"

        close_side = "sell" if pos == 1 else "buy"
        abs_qty = abs(qty)
        logger.warning(
            f"[LiveEngine:{self._strategy_id}] "
            f"РУЧНОЕ ЗАКРЫТИЕ: {close_side.upper()} {self._ticker} x{abs_qty}"
        )
        try:
            result = self._connector.place_order_result(
                account_id=self._account_id,
                ticker=self._ticker,
                side=close_side,
                quantity=abs_qty,
                order_type="market",
                board=self._board,
                agent_name=self._agent_name,
            )
            tid = result.transaction_id
            if tid:
                logger.warning(
                    f"[LiveEngine:{self._strategy_id}] "
                    f"Ордер ручного закрытия принят: tid={tid}"
                )
                return "success"
            else:
                logger.error(
                    f"[LiveEngine:{self._strategy_id}] "
                    f"Ордер ручного закрытия ОТКЛОНЁН/не подтверждён — "
                    f"позиция остаётся открытой! outcome={result.outcome.value} msg={result.message}"
                )
                return "close_failed"
        except Exception as e:
            logger.error(
                f"[LiveEngine:{self._strategy_id}] "
                f"Ошибка ручного закрытия: {e} — позиция остаётся открытой!"
            )
            return "close_failed"

    # === Poll loop ===

    def _get_lookback(self) -> int:
        if hasattr(self._module, "get_lookback"):
            try:
                return int(self._module.get_lookback(self._params))
            except Exception as e:
                logger.warning(
                    f"[LiveEngine:{self._strategy_id}] get_lookback() failed: {e}, "
                    f"using default={DEFAULT_STRATEGY_LOOKBACK}"
                )
        return DEFAULT_STRATEGY_LOOKBACK

    def _estimate_bars_per_day(self) -> int:
        bar_minutes = PERIOD_TO_BAR_MINUTES.get(self._period_str, 5)
        if bar_minutes >= 1440:
            return 1
        return max(1, math.floor(_TRADING_SESSION_MINUTES / bar_minutes))

    def _calculate_initial_history_days(self, lookback: int) -> int:
        required_bars = max(int(lookback or 0), 1)
        bars_per_day = self._estimate_bars_per_day()
        required_days = math.ceil((required_bars * _WARMUP_HISTORY_SAFETY_FACTOR) / bars_per_day)
        return max(required_days, _MIN_WARMUP_HISTORY_DAYS)

    def _calculate_incremental_history_days(self) -> int:
        return _INCREMENTAL_HISTORY_DAYS

    def _expand_history_days(self, current_days: int, required_bars: int, actual_bars: int) -> int:
        if actual_bars <= 0:
            return min(max(current_days * 2, _MIN_WARMUP_HISTORY_DAYS), _MAX_WARMUP_HISTORY_DAYS)
        deficit_ratio = required_bars / max(actual_bars, 1)
        growth_factor = min(max(deficit_ratio, 1.5), 3.0)
        return min(max(current_days + 1, math.ceil(current_days * growth_factor)), _MAX_WARMUP_HISTORY_DAYS)

    def _fetch_history_dataframe(self, days: int) -> tuple[bool, Optional[Exception], Optional[pd.DataFrame]]:
        result_lock = threading.Lock()
        result = {'df': None, 'error': None, 'done': False}

        def _fetch_history():
            try:
                df = self._connector.get_history(
                    ticker=self._ticker,
                    board=self._board,
                    period=self._period_str,
                    days=days,
                )
                with result_lock:
                    result['df'] = df
            except Exception as e:
                with result_lock:
                    result['error'] = e
            finally:
                with result_lock:
                    result['done'] = True

        connector_id = getattr(self._connector, '_connector_id', 'finam')
        timeout = 30 if connector_id == 'quik' else 10

        future = self._history_pool.submit(_fetch_history)
        try:
            future.result(timeout=timeout)
        except FuturesTimeoutError:
            pass
        except Exception:
            pass

        with result_lock:
            return result['done'], result['error'], result['df']

    def _load_history_dataframe(self, lookback: int) -> tuple[bool, Optional[Exception], Optional[pd.DataFrame]]:
        initial_load = self._get_last_bar_dt() is None
        if not initial_load:
            return self._fetch_history_dataframe(self._calculate_incremental_history_days())

        required_bars = max(int(lookback or 0), 1)
        days = self._calculate_initial_history_days(lookback)
        previous_count = -1
        last_result: tuple[bool, Optional[Exception], Optional[pd.DataFrame]] = (True, None, None)

        for _ in range(_MAX_WARMUP_FETCH_ATTEMPTS):
            done, fetch_error, df = self._fetch_history_dataframe(days)
            last_result = (done, fetch_error, df)
            if not done or fetch_error or df is None or df.empty:
                return last_result

            actual_bars = len(df.index)
            if actual_bars >= required_bars:
                return last_result
            if actual_bars <= previous_count or days >= _MAX_WARMUP_HISTORY_DAYS:
                logger.warning(
                    f"[LiveEngine:{self._strategy_id}] warmup incomplete: "
                    f"bars={actual_bars}/{required_bars}, days={days}"
                )
                return last_result

            next_days = self._expand_history_days(days, required_bars, actual_bars)
            logger.info(
                f"[LiveEngine:{self._strategy_id}] warmup refetch: "
                f"bars={actual_bars}/{required_bars}, days={days} -> {next_days}"
            )
            previous_count = actual_bars
            days = next_days

        return last_result

    def _build_market_data_envelope(self, source_dt=None) -> MarketDataEnvelope:
        source_ts = None
        if isinstance(source_dt, datetime):
            source_ts = source_dt.timestamp()
        stale_after_ms = max(int(self._poll_interval * 3000), 15000)
        return MarketDataEnvelope.build(
            source_ts=source_ts,
            receive_ts=time.time(),
            source_id=self._connector_id,
            stale_after_ms=stale_after_ms,
        )

    def _attach_market_data_envelopes(self, bars: list[dict]) -> list[dict]:
        stamped_bars = []
        for bar in bars:
            stamped = dict(bar)
            stamped["market_data"] = self._build_market_data_envelope(
                stamped.get("dt")
            ).to_dict()
            stamped_bars.append(stamped)
        return stamped_bars

    def _validate_bars(self, bars: list[dict]) -> tuple[bool, str]:
        if not bars:
            return False, "empty_bars"

        prev_dt = None
        seen_dt = set()
        for index, bar in enumerate(bars):
            bar_dt = bar.get("dt")
            if bar_dt is None:
                return False, f"missing_dt[{index}]"
            if bar_dt in seen_dt:
                return False, f"duplicate_bar_dt[{index}]"
            if prev_dt is not None and bar_dt <= prev_dt:
                return False, f"non_monotonic_bar_dt[{index}]"
            seen_dt.add(bar_dt)
            prev_dt = bar_dt

            try:
                open_price = float(bar.get("open", 0) or 0)
                high_price = float(bar.get("high", 0) or 0)
                low_price = float(bar.get("low", 0) or 0)
                close_price = float(bar.get("close", 0) or 0)
            except (TypeError, ValueError):
                return False, f"invalid_ohlc_type[{index}]"

            if min(open_price, high_price, low_price, close_price) <= 0:
                return False, f"non_positive_ohlc[{index}]"
            if high_price < max(open_price, low_price, close_price):
                return False, f"broken_high[{index}]"
            if low_price > min(open_price, high_price, close_price):
                return False, f"broken_low[{index}]"

        return True, ""

    def _poll_loop(self):
        """Основной цикл: загрузка истории → поллинг новых баров."""
        self._load_and_update()

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

        if self._stop_event.is_set():
            return

        if self._get_last_bar_dt() is None:
            try:
                if hasattr(self._connector, "clear_history_cache"):
                    self._connector.clear_history_cache(self._ticker, self._board)
            except Exception:
                pass

        done, fetch_error, df = self._load_history_dataframe(lookback)

        if not done:
            self._consecutive_timeouts += 1
            logger.warning(
                f"[LiveEngine:{self._strategy_id}] get_history тайм-аут "
                f"({self._consecutive_timeouts}/{self._MAX_CONSECUTIVE_TIMEOUTS}), пропускаем тик"
            )
            if self._consecutive_timeouts >= self._MAX_CONSECUTIVE_TIMEOUTS:
                logger.error(
                    f"[LiveEngine:{self._strategy_id}] {self._MAX_CONSECUTIVE_TIMEOUTS} "
                    f"тайм-аутов подряд — переход в degraded state (manual intervention required)"
                )
                self._sync_status = "stale"
                self._set_runtime_state(StrategyRuntimeState.DEGRADED.value)
                try:
                    from core.telegram_bot import notifier, EventCode
                    notifier.send(
                        EventCode.STRATEGY_CRASHED,
                        agent=self._strategy_id,
                        description=(
                            f"[{self._strategy_id}] {self._MAX_CONSECUTIVE_TIMEOUTS} тайм-аутов "
                            f"get_history подряд — стратегия в degraded state, "
                            f"требуется ручное вмешательство"
                        ),
                    )
                except Exception:
                    pass
            return
        else:
            self._consecutive_timeouts = 0

        if fetch_error:
            logger.error(
                f"[LiveEngine:{self._strategy_id}] Ошибка get_history: {fetch_error}"
            )
            return

        if df is None or df.empty:
            return

        bars = []
        for dt_idx, row in df.iterrows():
            dt = dt_idx.to_pydatetime() if hasattr(dt_idx, 'to_pydatetime') else dt_idx
            bars.append(_bar_from_row(row, dt))

        if not bars:
            return

        bars = self._attach_market_data_envelopes(bars)
        is_valid_bars, bars_error = self._validate_bars(bars)
        if not is_valid_bars:
            logger.error(
                f"[LiveEngine:{self._strategy_id}] invalid market data bars: {bars_error}"
            )
            self._require_manual_intervention(f"invalid_bars:{bars_error}")
            return

        newest_dt = bars[-1]["dt"]
        last_bar_dt = self._get_last_bar_dt()
        if last_bar_dt and newest_dt <= last_bar_dt:
            return

        self._last_price = bars[-1]["close"]
        self._last_price_envelope = MarketDataEnvelope(
            **dict(bars[-1].get("market_data") or self._build_market_data_envelope(newest_dt).to_dict())
        )

        if self._point_cost == 1.0:
            self._load_point_cost()

        self._record_equity()

        new_bar_dt = bars[-1]["dt"]

        updated, was_initial = self._update_bar_state(bars, new_bar_dt)
        if not updated:
            return

        if was_initial:
            logger.info(f"[LiveEngine:{self._strategy_id}] Загружено {len(bars)} баров, "
                        f"последний: {new_bar_dt}")
            return

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

        is_valid_bars, bars_error = self._validate_bars(bars)
        if not is_valid_bars:
            logger.error(
                f"[LiveEngine:{self._strategy_id}] _process_bar rejected invalid bars: {bars_error}"
            )
            self._require_manual_intervention(f"invalid_bars:{bars_error}")
            return

        if self._last_price <= 0 and closed_bars:
            self._last_price = float(closed_bars[-1].get("close", 0) or 0)
        if self._last_price_envelope is None and closed_bars:
            closed_envelope = closed_bars[-1].get("market_data")
            if isinstance(closed_envelope, dict):
                self._last_price_envelope = MarketDataEnvelope(**closed_envelope)
            else:
                self._last_price_envelope = self._build_market_data_envelope(
                    closed_bars[-1].get("dt")
                )

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

        if isinstance(signal, dict):
            signal.setdefault("signal_ts", time.time())
            if self._last_price_envelope is not None:
                signal.setdefault("market_data_envelope", self._last_price_envelope.to_dict())

        action = signal.get("action") if signal else None
        if action:
            self._last_signal_ts = time.monotonic()

            # Degraded state: запрещаем открывающие сделки при stale broker data
            if action in ("buy", "sell") and self._sync_status != "synced":
                logger.warning(
                    f"[LiveEngine:{self._strategy_id}] DEGRADED: sync_status={self._sync_status}, "
                    f"{action.upper()} отклонён — ожидаем resync с брокером"
                )
                return

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

            # Делегируем execute_signal стратегии или order_executor.
            # Custom execute_signal разрешён ТОЛЬКО для explicit adapters,
            # валидированных через core.strategy_loader.
            adapter_name = getattr(self._loaded, "custom_execution_adapter", None)
            adapter_actions = getattr(self._loaded, "custom_execution_actions", frozenset())
            has_custom_exec = hasattr(self._module, "execute_signal") and bool(adapter_name)

            if hasattr(self._module, "execute_signal") and not adapter_name:
                logger.error(
                    f"[LiveEngine:{self._strategy_id}] Стратегия определяет execute_signal, "
                    f"но не зарегистрирована как explicit execution adapter. "
                    f"Custom execution заблокирован — используем стандартный OrderExecutor"
                )

            if has_custom_exec:
                try:
                    signal_action = signal.get("action")
                    if signal_action not in adapter_actions:
                        logger.warning(
                            f"[LiveEngine:{self._strategy_id}] Некорректный action={signal_action!r} "
                            f"для adapter={adapter_name}, сигнал пропущен"
                        )
                        return
                    if signal_action in ("buy", "sell", "close"):
                        try:
                            signal_qty = int(signal.get("qty", 1))
                        except (TypeError, ValueError):
                            logger.warning(
                                f"[LiveEngine:{self._strategy_id}] Некорректный qty в сигнале "
                                f"custom adapter={adapter_name}: {signal.get('qty')!r}"
                            )
                            return
                        if signal_qty <= 0:
                            logger.warning(
                                f"[LiveEngine:{self._strategy_id}] qty <= 0 в сигнале "
                                f"custom adapter={adapter_name}: {signal_qty}"
                            )
                            return

                        if signal_action in ("buy", "sell"):
                            allowed, reason = self._custom_pretrade_risk_check(signal_action, signal_qty)
                            if not allowed:
                                logger.warning(
                                    f"[LiveEngine:{self._strategy_id}] RISK REJECT: {reason}, "
                                    f"custom {signal_action.upper()} x{signal_qty} отклонён"
                                )
                                return

                    custom_params = dict(self._params)
                    custom_params["_strategy_id"] = self._strategy_id
                    custom_params["_connector_id"] = self._connector_id
                    custom_params["_execution_adapter"] = adapter_name
                    custom_params["_pretrade_risk_check"] = self._custom_pretrade_risk_check
                    custom_params["_account_risk_check"] = self._custom_account_risk_check
                    custom_params["_reserve_capital"] = self._custom_reserve_capital
                    custom_params["_release_capital"] = self._custom_release_capital
                    self._module.execute_signal(
                        signal, self._connector, custom_params, self._account_id
                    )
                except Exception as e:
                    logger.error(f"[LiveEngine:{self._strategy_id}] "
                                 f"execute_signal error: {e}\n{traceback.format_exc()}")
            else:
                # Делегируем order_executor
                self._order_executor.execute_signal(signal)

    # === Legacy прокси-методы для обратной совместимости ===

    def _on_circuit_break(self):
        """Вызывается из OrderExecutor при срабатывании circuit breaker.

        SAFETY: circuit breaker запрещает новые рисковые действия,
        но НИКОГДА не закрывает позицию автоматически.
        Оператор получает уведомление и должен действовать вручную.
        """
        logger.error(
            f"[LiveEngine:{self._strategy_id}] CIRCUIT BREAKER: "
            f"{self._risk_guard.consecutive_failures} ошибок подряд — "
            f"новые ордера заблокированы, позиция сохранена. "
            f"Требуется ручное вмешательство."
        )
        self._sync_status = "stale"
        self._set_runtime_state(StrategyRuntimeState.MANUAL_INTERVENTION_REQUIRED.value)
        try:
            from core.telegram_bot import notifier, EventCode
            notifier.send(
                EventCode.STRATEGY_CRASHED,
                agent=self._strategy_id,
                description=(
                    f"[{self._strategy_id}] Circuit breaker сработал: "
                    f"{self._risk_guard.consecutive_failures} ошибок подряд. "
                    f"Новые ордера заблокированы, позиция сохранена. "
                    f"Требуется ручное вмешательство."
                ),
            )
        except Exception:
            pass

    def _record_failure(self):
        """Прокси к risk_guard.record_failure()."""
        if self._risk_guard.record_failure():
            self._on_circuit_break()

    def _record_success(self):
        """Прокси к risk_guard.record_success()."""
        self._risk_guard.record_success()

    def _check_risk_limits(self, action: str, qty: int) -> tuple[bool, str]:
        """Прокси к risk_guard.check_risk_limits()."""
        return self._risk_guard.check_risk_limits(action, qty)

    def _record_trade(self, side: str, qty: int, price: float, comment: str,
                      order_type: str = "market", order_ref: str = "",
                      correlation_id: str = ""):
        """Прокси к trade_recorder.record_trade()."""
        self._trade_recorder.record_trade(
            side, qty, price, comment, order_type, order_ref, correlation_id
        )

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
