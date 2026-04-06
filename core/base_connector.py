from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
import threading
import time
import math
from typing import Callable, Literal, Optional

# Общие типы для side / action / order_type
Side = Literal["buy", "sell"]
Action = Literal["buy", "sell", "close"]
OrderType = Literal["market", "limit"]
OrderMode = Literal["market", "limit", "limit_price", "limit_book"]
from loguru import logger


class OrderOutcome(str, Enum):
    SUCCESS = "success"
    NOT_FOUND = "not_found"
    REJECTED = "rejected"
    TRANSPORT_ERROR = "transport_error"
    STALE_STATE = "stale_state"


@dataclass
class OrderResult:
    outcome: OrderOutcome
    transaction_id: str = ""
    message: str = ""

    @property
    def is_success(self) -> bool:
        return self.outcome == OrderOutcome.SUCCESS


@dataclass
class MarketDataEnvelope:
    source_ts: float
    receive_ts: float
    age_ms: int
    source_id: str
    status: str = "unknown"

    @classmethod
    def build(
        cls,
        source_ts: float | None = None,
        receive_ts: float | None = None,
        source_id: str = "",
        stale_after_ms: int = 0,
        status: str = "",
    ) -> "MarketDataEnvelope":
        receive_ts = float(receive_ts if receive_ts is not None else time.time())
        source_ts = float(source_ts if source_ts is not None else receive_ts)
        age_ms = max(int((receive_ts - source_ts) * 1000), 0)
        if not status:
            status = "stale" if stale_after_ms > 0 and age_ms > stale_after_ms else "fresh"
        return cls(
            source_ts=source_ts,
            receive_ts=receive_ts,
            age_ms=age_ms,
            source_id=str(source_id or ""),
            status=str(status or "unknown"),
        )

    def to_dict(self) -> dict:
        return {
            "source_ts": self.source_ts,
            "receive_ts": self.receive_ts,
            "age_ms": self.age_ms,
            "source_id": self.source_id,
            "status": self.status,
        }


class BaseConnector(ABC):

    def __init__(self):
        self._on_connect: Optional[Callable] = None
        self._on_disconnect: Optional[Callable] = None
        self._on_reconnect: Optional[Callable] = None
        self._on_error: Optional[Callable] = None
        self._on_positions_update: Optional[Callable] = None
        self._connect_listeners: list[Callable] = []
        self._disconnect_listeners: list[Callable] = []
        self._reconnect_listeners: list[Callable] = []
        self._error_listeners: list[Callable[[str], None]] = []
        self._positions_listeners: list[Callable] = []
        self._reconnect_attempts: int  = 5
        self._reconnect_delay:    int  = 5   # базовая задержка (секунд)
        self._stop_reconnect = threading.Event()
        self._reconnect_start_lock = threading.Lock()
        self._health_check_event = threading.Event()
        self._health_check_interval: int = 300  # периодическая глубокая проверка (секунд)

    # ── Обязательный интерфейс ──────────────────────────────────────────

    @abstractmethod
    def connect(self) -> bool: ...

    @abstractmethod
    def get_last_price(self, ticker: str, board: str = "TQBR") -> Optional[float]:
        ...

    @abstractmethod
    def disconnect(self): ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def place_order(
        self,
        account_id: str,
        ticker: str,
        side: Side,
        quantity: int,
        order_type: OrderType,
        price: float = 0.0,
        board: str = "TQBR",
        agent_name: str = "",
    ) -> Optional[str]: ...  # transaction_id или None при ошибке

    @abstractmethod
    def cancel_order(self, order_id: str, account_id: str) -> bool: ...

    @abstractmethod
    def get_positions(self, account_id: str) -> list[dict]: ...

    def get_all_positions(self) -> dict:
        """Возвращает все позиции в формате {account_id: [positions]}."""
        return {}

    def get_free_money(self, account_id: str) -> Optional[float]:
        """Свободные средства на счёте."""
        return None

    def get_client_limits(self, client_id: str) -> Optional[dict]:
        """Лимиты клиента (money_free, money_current, coverage и т.д.)."""
        return None

    @abstractmethod
    def get_accounts(self) -> list[dict]: ...

    @abstractmethod
    def get_order_book(self, board: str, ticker: str, depth: int = 10) -> Optional[dict]:
        """
        Получить стакан заявок.
        
        Args:
            board: Код режима торгов (TQBR, SPBFUT и т.д.)
            ticker: Тикер инструмента
            depth: Глубина стакана (количество уровней)
            
        Returns:
            dict: {"bids": [(price, volume), ...], "asks": [(price, volume), ...]}
                  bids отсортированы по убыванию цены, asks по возрастанию
            None: если стакан недоступен
        """
        ...

    @abstractmethod
    def close_position(
        self,
        account_id: str,
        ticker: str,
        quantity: int = 0,
        agent_name: str = "",
    ) -> Optional[str]:
        """Закрыть позицию по рыночной цене.

        Канонический контракт:
        - Определяет сторону (buy/sell) автоматически по текущей позиции.
        - quantity=0 → закрыть всю позицию целиком.
        - 0 < quantity <= abs(текущая позиция) → закрыть указанное кол-во лотов.
        - Если позиция не найдена или нулевая — возвращает None (не ошибка).

        Возвращает:
            str — transaction_id размещённого ордера при успешной отправке.
            None — позиция не найдена / ордер не принят.
        """
        ...

    def chase_order(
        self,
        account_id: str,
        ticker: str,
        side: str,
        quantity: int,
        board: str = "TQBR",
        agent_name: str = "",
    ):
        raise NotImplementedError("chase_order not supported by this connector")

    def place_order_result(
        self,
        account_id: str,
        ticker: str,
        side: Side,
        quantity: int,
        order_type: OrderType,
        price: float = 0.0,
        board: str = "TQBR",
        agent_name: str = "",
    ) -> OrderResult:
        transaction_id = self.place_order(
            account_id=account_id,
            ticker=ticker,
            side=side,
            quantity=quantity,
            order_type=order_type,
            price=price,
            board=board,
            agent_name=agent_name,
        )
        if transaction_id:
            return OrderResult(OrderOutcome.SUCCESS, transaction_id=str(transaction_id))
        return OrderResult(OrderOutcome.REJECTED, message="legacy_place_order_returned_none")

    def cancel_order_result(self, order_id: str, account_id: str) -> OrderResult:
        canceled = self.cancel_order(order_id, account_id)
        if canceled:
            return OrderResult(OrderOutcome.SUCCESS, transaction_id=str(order_id))
        return OrderResult(
            OrderOutcome.REJECTED,
            transaction_id=str(order_id),
            message="legacy_cancel_order_returned_false",
        )

    def close_position_result(
        self,
        account_id: str,
        ticker: str,
        quantity: int = 0,
        agent_name: str = "",
    ) -> OrderResult:
        transaction_id = self.close_position(
            account_id=account_id,
            ticker=ticker,
            quantity=quantity,
            agent_name=agent_name,
        )
        if transaction_id:
            return OrderResult(OrderOutcome.SUCCESS, transaction_id=str(transaction_id))
        return OrderResult(OrderOutcome.REJECTED, message="legacy_close_position_returned_none")

    # ── Общие утилиты ───────────────────────────────────────────────────

    def configure_reconnect(self, attempts: int, delay: int):
        self._reconnect_attempts = attempts
        self._reconnect_delay    = delay

    def request_health_check(self):
        """Запросить немедленную проверку соединения из reconnect-loop.

        Вызывать из путей обработки ошибок операций (place_order, get_history
        и т.д.), чтобы не ждать следующего цикла опроса.
        """
        self._health_check_event.set()

    def health_check(self) -> bool:
        """Глубокая проверка соединения.

        По умолчанию делегирует is_connected(). Подклассы переопределяют
        для более надёжной проверки (ping, lightweight request и т.д.).
        """
        return self.is_connected()

    def _fire_event(self, event_type: str, *args):
        """Безопасный вызов колбэка + всех подписчиков по типу события."""
        mapping = {
            'connect': (self._on_connect, self._connect_listeners),
            'disconnect': (self._on_disconnect, self._disconnect_listeners),
            'error': (self._on_error, self._error_listeners),
            'reconnect': (self._on_reconnect, self._reconnect_listeners),
            'positions': (self._on_positions_update, self._positions_listeners),
        }
        cb, listeners = mapping.get(event_type, (None, []))
        if callable(cb):
            try:
                cb(*args)
            except Exception as e:
                logger.error(f"[{self.__class__.__name__}] callback error: {e}")
        for listener in list(listeners):
            try:
                listener(*args)
            except Exception as e:
                logger.error(f"[{self.__class__.__name__}] listener error: {e}")

    def _fire(self, cb: Optional[Callable], *args):
        """Безопасный вызов колбэка + всех подписчиков (обратная совместимость)."""
        if callable(cb):
            try:
                cb(*args)
            except Exception as e:
                logger.error(f"[{self.__class__.__name__}] callback error: {e}")
        # Вызываем соответствующие списки слушателей по типу колбэка
        listeners: list[Callable] = []
        if cb is self._on_connect:
            listeners = list(self._connect_listeners)
        elif cb is self._on_disconnect:
            listeners = list(self._disconnect_listeners)
        elif cb is self._on_reconnect:
            listeners = list(self._reconnect_listeners)
        elif cb is self._on_error:
            listeners = list(self._error_listeners)
        elif cb is self._on_positions_update:
            listeners = list(self._positions_listeners)

        for listener in listeners:
            try:
                listener(*args)
            except Exception as e:
                logger.error(f"[{self.__class__.__name__}] listener error: {e}")

    def on_connect(self, callback: Callable[[], None]):
        self._on_connect = callback

    def on_disconnect(self, callback: Callable[[], None]):
        self._on_disconnect = callback

    def on_reconnect(self, callback: Callable[[], None]):
        self._on_reconnect = callback

    def on_error(self, callback: Callable[[str], None]):
        self._on_error = callback

    def on_positions_update(self, callback: Callable[[], None]):
        self._on_positions_update = callback

    # Подписки на множественные слушатели
    def subscribe_connect(self, callback: Callable[[], None]):
        if callback not in self._connect_listeners:
            self._connect_listeners.append(callback)

    def unsubscribe_connect(self, callback: Callable[[], None]):
        self._connect_listeners = [cb for cb in self._connect_listeners if cb is not callback]

    def subscribe_disconnect(self, callback: Callable[[], None]):
        if callback not in self._disconnect_listeners:
            self._disconnect_listeners.append(callback)

    def unsubscribe_disconnect(self, callback: Callable[[], None]):
        self._disconnect_listeners = [cb for cb in self._disconnect_listeners if cb is not callback]

    def subscribe_reconnect(self, callback: Callable[[], None]):
        if callback not in self._reconnect_listeners:
            self._reconnect_listeners.append(callback)

    def unsubscribe_reconnect(self, callback: Callable[[], None]):
        self._reconnect_listeners = [cb for cb in self._reconnect_listeners if cb is not callback]

    def subscribe_error(self, callback: Callable[[str], None]):
        if callback not in self._error_listeners:
            self._error_listeners.append(callback)

    def unsubscribe_error(self, callback: Callable[[str], None]):
        self._error_listeners = [cb for cb in self._error_listeners if cb is not callback]

    def subscribe_positions(self, callback: Callable[[], None]):
        if callback not in self._positions_listeners:
            self._positions_listeners.append(callback)

    def unsubscribe_positions(self, callback: Callable[[], None]):
        self._positions_listeners = [cb for cb in self._positions_listeners if cb is not callback]

    def off_positions_update(self):
        """Отписка от обновлений позиций."""
        self._on_positions_update = None
        self._positions_listeners = []

    # ── Авторекконект с экспоненциальным backoff ─────────────────────────

    def start_reconnect_loop(self):
        """Запускает фоновый поток переподключения при обрыве.

        Идемпотентен: если поток уже запущен — повторный вызов игнорируется.
        Перед запуском сбрасывает флаг _stop_reconnect для корректной работы
        после планового отключения по расписанию.
        """
        with self._reconnect_start_lock:
            # Сбрасываем флаг перед запуском — это критично для работы после disconnect()
            self._stop_reconnect.clear()

            if hasattr(self, "_reconnect_thread") and self._reconnect_thread.is_alive():
                logger.debug(f"[{self.__class__.__name__}] reconnect-loop уже запущен")
                return
            self._reconnect_thread = threading.Thread(
                target=self._reconnect_loop, daemon=True, name="reconnect-loop"
            )
            self._reconnect_thread.start()

    def _on_reconnect_success(self):
        """Hook: вызывается после успешного переподключения.

        Подклассы переопределяют для force-check ордеров и прочих действий.
        """
        pass

    def _reconnect_loop(self):
        from loguru import logger
        from core.scheduler import is_in_schedule

        name = self.__class__.__name__
        connector_id = name.removesuffix('Connector').lower()
        attempt = 0
        last_deep_check = time.time()

        while not self._stop_reconnect.is_set():
            # Ждём polling-интервал, stop-сигнал или принудительную проверку
            self._health_check_event.wait(timeout=2)
            forced = self._health_check_event.is_set()
            self._health_check_event.clear()

            if self._stop_reconnect.is_set():
                break

            now = time.time()
            connected = self.is_connected()

            # Периодическая глубокая проверка даже если is_connected() == True
            if connected and (now - last_deep_check >= self._health_check_interval or forced):
                reason = "forced" if forced else "periodic"
                if not self.health_check():
                    logger.warning(f"[{name}] Глубокая проверка ({reason}) выявила обрыв соединения")
                    connected = False
                else:
                    last_deep_check = now

            if connected:
                attempt = 0
                continue

            if not is_in_schedule(connector_id):
                if attempt != 0:
                    logger.info(f'[{name}] Вне окна расписания — счётчик переподключения сброшен')
                    attempt = 0
                self._stop_reconnect.wait(30)
                continue

            if attempt >= self._reconnect_attempts:
                logger.error(
                    f"[{name}] Исчерпаны попытки переподключения ({attempt}), cooldown 120с"
                )
                self._fire_event(
                    'error',
                    f"Не удалось переподключиться после {attempt} попыток, ожидаем cooldown"
                )
                self._stop_reconnect.wait(120)
                if self._stop_reconnect.is_set():
                    break
                attempt = 0
                logger.info(f"[{name}] Cooldown завершён, сбрасываем счётчик попыток")
                continue

            attempt += 1
            reason = "health-check fail" if forced else "polling detected disconnect"
            logger.info(
                f"[{name}] Переподключение {attempt}/{self._reconnect_attempts} "
                f"(причина: {reason})…"
            )
            try:
                self.connect()
                if self.is_connected():
                    logger.info(f"[{name}] Успешное переподключение")
                    self._fire_event('reconnect')
                    last_deep_check = time.time()
                    try:
                        self._on_reconnect_success()
                    except Exception as e:
                        logger.warning(f"[{name}] _on_reconnect_success error: {e}")
            except Exception as e:
                logger.error(f"[{name}] Исключение при connect(): {e}")
            if not self.is_connected():
                delay = min(self._reconnect_delay * (2 ** (attempt - 1)), 120)
                logger.debug(f"[{name}] Следующая попытка через {delay}с")
                self._stop_reconnect.wait(delay)