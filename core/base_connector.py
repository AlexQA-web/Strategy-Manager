from abc import ABC, abstractmethod
import threading
import time
import math
from typing import Callable, Optional


class BaseConnector(ABC):

    def __init__(self):
        self._on_connect: Optional[Callable] = None
        self._on_disconnect: Optional[Callable] = None
        self._on_reconnect: Optional[Callable] = None
        self._on_error: Optional[Callable] = None
        self._on_positions_update: Optional[Callable] = None
        self._reconnect_attempts: int  = 5
        self._reconnect_delay:    int  = 5   # базовая задержка (секунд)
        self._stop_reconnect = threading.Event()

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
        side: str,          # "buy" | "sell"
        quantity: int,
        order_type: str,    # "market" | "limit"
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
    ) -> bool: ...

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

    # ── Общие утилиты ───────────────────────────────────────────────────

    def configure_reconnect(self, attempts: int, delay: int):
        self._reconnect_attempts = attempts
        self._reconnect_delay    = delay

    def _fire(self, cb: Optional[Callable], *args):
        """Безопасный вызов колбэка."""
        if cb:
            try:
                cb(*args)
            except Exception as e:
                from loguru import logger
                logger.error(f"[{self.__class__.__name__}] callback error: {e}")

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

    def off_positions_update(self):
        """Отписка от обновлений позиций."""
        self._on_positions_update = None

    # ── Авторекконект с экспоненциальным backoff ─────────────────────────

    def start_reconnect_loop(self):
        """Запускает фоновый поток переподключения при обрыве.

        Идемпотентен: если поток уже запущен — повторный вызов игнорируется.
        """
        if hasattr(self, "_reconnect_thread") and self._reconnect_thread.is_alive():
            return
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True, name="reconnect-loop"
        )
        self._reconnect_thread.start()

    def _reconnect_loop(self):
        from loguru import logger
        name = self.__class__.__name__
        attempt = 0
        while not self._stop_reconnect.is_set():
            time.sleep(2)
            if self.is_connected():
                attempt = 0
                continue
            if attempt >= self._reconnect_attempts:
                logger.error(f"[{name}] Исчерпаны попытки переподключения")
                self._fire(
                    self._on_error,
                    f"Не удалось переподключиться после {attempt} попыток"
                )
                break
            attempt += 1
            logger.info(f"[{name}] Переподключение {attempt}/{self._reconnect_attempts}…")
            try:
                self.connect()
                if self.is_connected():
                    logger.info(f"[{name}] Успешное переподключение")
                    self._fire(self._on_reconnect)
            except Exception as e:
                logger.error(f"[{name}] Исключение при connect(): {e}")
            if not self.is_connected():
                delay = min(self._reconnect_delay * (2 ** (attempt - 1)), 120)
                logger.debug(f"[{name}] Следующая попытка через {delay}с")
                self._stop_reconnect.wait(delay)