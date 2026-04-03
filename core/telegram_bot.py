import asyncio
import threading
import traceback
from datetime import datetime
from typing import Optional
from loguru import logger

try:
    from telegram import Bot
    from telegram.error import TelegramError
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    logger.warning("python-telegram-bot не установлен. Уведомления отключены.")

from core.storage import get_setting, get_bool_setting
from core.ntfy_notifier import ntfy_notifier


# ─────────────────────────────────────────────
# Коды событий — каждый код = конкретное сообщение
# ─────────────────────────────────────────────
class EventCode:
    # Позиции
    MISSED_ENTRY        = "MISSED_ENTRY"        # Пропущен вход
    MISSED_EXIT         = "MISSED_EXIT"         # Пропущен выход
    POSITION_OPENED     = "POSITION_OPENED"     # Позиция открыта
    POSITION_CLOSED     = "POSITION_CLOSED"     # Позиция закрыта
    STOP_LOSS_HIT       = "STOP_LOSS_HIT"       # Сработал стоп-лосс
    TAKE_PROFIT_HIT     = "TAKE_PROFIT_HIT"     # Сработал тейк-профит

    # Ордера
    ORDER_PLACED        = "ORDER_PLACED"        # Ордер выставлен
    ORDER_FILLED        = "ORDER_FILLED"        # Ордер исполнен
    ORDER_REJECTED      = "ORDER_REJECTED"      # Ордер отклонён брокером
    ORDER_TIMEOUT       = "ORDER_TIMEOUT"       # Ордер не исполнен вовремя
    ORDER_PARTIAL_FILL  = "ORDER_PARTIAL_FILL"  # Частичное исполнение

    # Коннектор
    CONNECTOR_CONNECTED     = "CONNECTOR_CONNECTED"     # Подключён к Финам
    CONNECTOR_DISCONNECTED  = "CONNECTOR_DISCONNECTED"  # Отключён от Финам
    CONNECTOR_ERROR         = "CONNECTOR_ERROR"         # Ошибка соединения
    CONNECTOR_RECONNECTING  = "CONNECTOR_RECONNECTING"  # Переподключение

    # Стратегии
    STRATEGY_STARTED    = "STRATEGY_STARTED"    # Стратегия запущена
    STRATEGY_STOPPED    = "STRATEGY_STOPPED"    # Стратегия остановлена
    STRATEGY_ERROR      = "STRATEGY_ERROR"      # Ошибка внутри стратегии
    STRATEGY_CRASHED    = "STRATEGY_CRASHED"    # Стратегия упала (критично)

    # Расписание
    SCHEDULE_CONNECT    = "SCHEDULE_CONNECT"    # Плановое подключение
    SCHEDULE_DISCONNECT = "SCHEDULE_DISCONNECT" # Плановое отключение

    # Система
    APP_STARTED         = "APP_STARTED"         # Приложение запущено
    APP_STOPPED         = "APP_STOPPED"         # Приложение остановлено


# ─────────────────────────────────────────────
# Шаблоны сообщений
# ─────────────────────────────────────────────
_TEMPLATES: dict[str, str] = {
    EventCode.MISSED_ENTRY: (
        "⚠️ <b>Пропущен вход в позицию</b>\n"
        "Агент: <code>{agent}</code>\n"
        "Сигнал: <b>{signal}</b> {ticker}\n"
        "Причина: {reason}"
    ),
    EventCode.MISSED_EXIT: (
        "⚠️ <b>Пропущен выход из позиции</b>\n"
        "Агент: <code>{agent}</code>\n"
        "Тикер: <b>{ticker}</b>\n"
        "Причина: {reason}"
    ),
    EventCode.POSITION_OPENED: (
        "✅ <b>Позиция открыта</b>\n"
        "Агент: <code>{agent}</code>\n"
        "Тикер: <b>{ticker}</b> | Направление: {side}\n"
        "Объём: {quantity} | Цена: {price}"
    ),
    EventCode.POSITION_CLOSED: (
        "🔒 <b>Позиция закрыта</b>\n"
        "Агент: <code>{agent}</code>\n"
        "Тикер: <b>{ticker}</b>\n"
        "P&L: {pnl}"
    ),
    EventCode.STOP_LOSS_HIT: (
        "🔴 <b>Сработал стоп-лосс</b>\n"
        "Агент: <code>{agent}</code>\n"
        "Тикер: <b>{ticker}</b> | Убыток: {loss_pct}%\n"
        "Цена входа: {entry_price} → Цена выхода: {exit_price}"
    ),
    EventCode.TAKE_PROFIT_HIT: (
        "💰 <b>Сработал тейк-профит</b>\n"
        "Агент: <code>{agent}</code>\n"
        "Тикер: <b>{ticker}</b> | Прибыль: +{profit_pct}%\n"
        "Цена входа: {entry_price} → Цена выхода: {exit_price}"
    ),
    EventCode.ORDER_PLACED: (
        "📝 <b>Ордер выставлен</b>\n"
        "Агент: <code>{agent}</code>\n"
        "Детали: {description}"
    ),
    EventCode.ORDER_FILLED: (
        "✅ <b>Ордер исполнен</b>\n"
        "Агент: <code>{agent}</code>\n"
        "Детали: {description}"
    ),
    EventCode.ORDER_REJECTED: (
        "❌ <b>Ордер отклонён брокером</b>\n"
        "Агент: <code>{agent}</code>\n"
        "Тикер: <b>{ticker}</b> | Объём: {quantity}\n"
        "Причина: {reason}\n"
        "Код ошибки: <code>{error_code}</code>"
    ),
    EventCode.ORDER_TIMEOUT: (
        "⏱ <b>Ордер не исполнен вовремя</b>\n"
        "Агент: <code>{agent}</code>\n"
        "Тикер: <b>{ticker}</b> | Тип: {order_type}\n"
        "Ордер отменён автоматически"
    ),
    EventCode.ORDER_PARTIAL_FILL: (
        "🔶 <b>Частичное исполнение ордера</b>\n"
        "Агент: <code>{agent}</code>\n"
        "Тикер: <b>{ticker}</b>\n"
        "Запрошено: {requested} | Исполнено: {filled}"
    ),
    EventCode.CONNECTOR_CONNECTED: (
        "🟢 <b>Коннектор подключён к Финам</b>\n"
        "Счёт: <code>{account}</code>\n"
        "Активных стратегий: {active_count}\n"
        "Время: {time}"
    ),
    EventCode.CONNECTOR_DISCONNECTED: (
        "🔌 <b>Коннектор отключён от Финам</b>\n"
        "Причина: {reason}\n"
        "Время: {time}"
    ),
    EventCode.CONNECTOR_ERROR: (
        "🚫 <b>Ошибка соединения с Финам</b>\n"
        "Описание: {description}\n"
        "Код: <code>{error_code}</code>"
    ),
    EventCode.CONNECTOR_RECONNECTING: (
        "🔄 <b>Переподключение к Финам...</b>\n"
        "Попытка: {attempt} из {max_attempts}"
    ),
    EventCode.STRATEGY_STARTED: (
        "▶️ <b>Стратегия запущена</b>\n"
        "Агент: <code>{agent}</code>\n"
        "Счёт: {account}"
    ),
    EventCode.STRATEGY_STOPPED: (
        "⏹ <b>Стратегия остановлена</b>\n"
        "Агент: <code>{agent}</code>\n"
        "Причина: {reason}"
    ),
    EventCode.STRATEGY_ERROR: (
        "🔥 <b>Ошибка в стратегии</b>\n"
        "Агент: <code>{agent}</code>\n"
        "Описание: {description}\n"
        "Стратегия продолжает работу"
    ),
    EventCode.STRATEGY_CRASHED: (
        "🚨 <b>КРИТИЧНО: Стратегия упала</b>\n"
        "Агент: <code>{agent}</code>\n"
        "Описание: {description}\n"
        "<b>Стратегия остановлена автоматически</b>\n"
        "<pre>{traceback}</pre>"
    ),
    EventCode.SCHEDULE_CONNECT: (
        "📅 <b>Плановое подключение</b>\n"
        "Агент: <code>{agent}</code>\n"
        "По расписанию: {scheduled_time}"
    ),
    EventCode.SCHEDULE_DISCONNECT: (
        "📅 <b>Плановое отключение</b>\n"
        "Агент: <code>{agent}</code>\n"
        "По расписанию: {scheduled_time}"
    ),
    EventCode.APP_STARTED: (
        "🚀 <b>Trading Manager запущен</b>\n"
        "Версия: {version}\n"
        "Время: {time}"
    ),
    EventCode.APP_STOPPED: (
        "🛑 <b>Trading Manager остановлен</b>\n"
        "Время: {time}"
    ),
}


# ─────────────────────────────────────────────
# Уровни уведомлений
# ─────────────────────────────────────────────
_ERROR_CODES = {
    EventCode.MISSED_ENTRY, EventCode.MISSED_EXIT,
    EventCode.ORDER_REJECTED, EventCode.ORDER_TIMEOUT,
    EventCode.CONNECTOR_ERROR, EventCode.STRATEGY_ERROR,
    EventCode.STRATEGY_CRASHED, EventCode.STOP_LOSS_HIT,
}

_CRITICAL_CODES = {
    EventCode.STRATEGY_CRASHED,
    EventCode.CONNECTOR_ERROR,
}


class NotificationLevel:
    ALL = "all"
    ERRORS_ONLY = "errors"
    CRITICAL_ONLY = "critical"


# ─────────────────────────────────────────────
# Основной класс бота
# ─────────────────────────────────────────────
class TelegramNotifier:

    def __init__(self):
        self._bot: Optional[Bot] = None
        self._token: Optional[str] = None
        self._chat_id: Optional[str] = None
        self._level: str = NotificationLevel.ALL
        self._enabled: bool = False
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="TelegramLoop"
        )
        self._thread.start()

    def _run_loop(self):
        """Запускает asyncio event loop в отдельном потоке."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def configure(self, token: str, chat_id: str,
                  level: str = NotificationLevel.ALL,
                  enabled: bool = True):
        """Настраивает бота. Вызывается при сохранении настроек."""
        if not TELEGRAM_AVAILABLE:
            logger.warning("Telegram недоступен — пакет не установлен")
            return

        self._token = token.strip()
        self._chat_id = chat_id.strip()
        self._level = level
        self._bot = Bot(token=self._token)
        self._enabled = bool(enabled)
        logger.info(f"Telegram настроен. Уровень уведомлений: {level}, enabled={self._enabled}")

    def load_from_settings(self):
        """Загружает конфигурацию из settings.json."""
        token = get_setting("telegram_token")
        chat_id = get_setting("telegram_chat_id")
        level = get_setting("telegram_level", NotificationLevel.ALL)
        tg_enabled = get_bool_setting("telegram_enabled")

        if token and chat_id and tg_enabled:
            self.configure(token, chat_id, level, enabled=True)
        else:
            self._enabled = False
            logger.info("Telegram выключен или не настроен (нет токена/chat_id или disabled)")
        
        # Загружаем настройки для NTFY
        ntfy_notifier.load_from_settings()

    def send(self, event_code: str, **kwargs) -> bool:
        """
        Главный метод отправки уведомлений.

        Пример:
            notifier.send(EventCode.MISSED_ENTRY,
                          agent="Momentum_v2",
                          signal="BUY",
                          ticker="SBER",
                          reason="нет ликвидности")
        """
        # Проверяем, нужно ли отправлять уведомление
        if not self._should_send(event_code):
            return False

        template = _TEMPLATES.get(event_code)
        if not template:
            logger.warning(f"Нет шаблона для события: {event_code}")
            return False

        # Добавляем timestamp если не передан
        kwargs.setdefault("time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        try:
            # Обрезаем traceback до 800 символов чтобы не превышать лимит Telegram
            if "traceback" in kwargs and len(kwargs["traceback"]) > 800:
                kwargs["traceback"] = kwargs["traceback"][:800] + "\n...[обрезано]"

            text = template.format_map(_SafeDict(kwargs))
        except Exception as e:
            logger.error(f"Ошибка форматирования шаблона [{event_code}]: {e}")
            return False

        # Отправляем в оба канала, если они включены
        sent_anywhere = False
        
        # Отправка в Telegram
        if self._enabled:
            future = asyncio.run_coroutine_threadsafe(
                self._send_message(text), self._loop
            )
            future.add_done_callback(lambda f: self._on_send_done(f, event_code))
            sent_anywhere = True
            
        # Отправка в NTFY
        if self._ntfy_enabled():
            ntfy_success = ntfy_notifier.send(text)
            if ntfy_success:
                sent_anywhere = True

        return sent_anywhere

    def send_raw(self, text: str) -> bool:
        """Отправляет произвольный текст без шаблона."""
        sent_anywhere = False
        
        # Отправка в Telegram
        if self._enabled:
            future = asyncio.run_coroutine_threadsafe(
                self._send_message(text), self._loop
            )
            future.add_done_callback(lambda f: self._on_send_done(f, "raw"))
            sent_anywhere = True
            
        # Отправка в NTFY
        if self._ntfy_enabled():
            ntfy_success = ntfy_notifier.send(text)
            if ntfy_success:
                sent_anywhere = True

        return sent_anywhere

    def _ntfy_enabled(self) -> bool:
        """Проверяет, включена ли отправка в NTFY."""
        return get_bool_setting("ntfy_enabled")

    async def test_connection(self) -> tuple[bool, str]:
        """
        Проверяет соединение с Telegram.
        Возвращает (успех: bool, сообщение: str).
        """
        if not self._bot:
            return False, "Бот не настроен"
        try:
            me = await self._bot.get_me()
            await self._send_message(
                f"✅ <b>Тест соединения успешен</b>\n"
                f"Бот: @{me.username}\n"
                f"Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            return True, f"Подключено. Бот: @{me.username}"
        except TelegramError as e:
            return False, f"Ошибка Telegram: {e}"
        except Exception as e:
            return False, f"Неизвестная ошибка: {e}"

    def test_connection_sync(self) -> tuple[bool, str]:
        """Синхронная обёртка для вызова из UI (не async)."""
        future = asyncio.run_coroutine_threadsafe(
            self.test_connection(), self._loop
        )
        try:
            return future.result(timeout=10)
        except TimeoutError:
            return False, "Таймаут подключения (10 сек)"
        except Exception as e:
            return False, str(e)

    # ─────────────────────────────────────────────
    # Внутренние методы
    # ─────────────────────────────────────────────

    async def _send_message(self, text: str):
        """Отправляет сообщение через Telegram Bot API."""
        await self._bot.send_message(
            chat_id=self._chat_id,
            text=text,
            parse_mode="HTML",
        )

    def _on_send_done(self, future, event_code: str):
        """Callback после отправки — логирует ошибку если не удалось."""
        try:
            future.result()
        except TelegramError as e:
            logger.error(f"Telegram: не удалось отправить [{event_code}]: {e}")
        except RuntimeError:
            # interpreter shutdown — игнорируем, программа завершается
            pass
        except Exception as e:
            logger.error(f"Telegram: неизвестная ошибка [{event_code}]: {e}")

    def _should_send(self, event_code: str) -> tuple[bool, bool]:
        """Проверяет нужно ли отправлять событие по явной настройке или уровню уведомлений.
        
        Returns:
            (send_telegram: bool, send_ntfy: bool)
        """
        telegram_key = f"notify_telegram_{event_code}"
        ntfy_key = f"notify_ntfy_{event_code}"
        
        telegram_notify = get_setting(telegram_key)
        ntfy_notify = get_setting(ntfy_key)
        
        tg_global_enabled = get_bool_setting("telegram_enabled")
        ntfy_global_enabled = get_bool_setting("ntfy_enabled")

        def _is_notify_enabled(notify_val) -> bool:
            if notify_val is None:
                return True  # нет явной настройки — используем глобальную
            if isinstance(notify_val, bool):
                return notify_val
            return str(notify_val).lower() in ("true", "1", "yes", "on")

        telegram_enabled = self._enabled and tg_global_enabled and _is_notify_enabled(telegram_notify)
        ntfy_enabled = ntfy_global_enabled and self._ntfy_enabled() and _is_notify_enabled(ntfy_notify)
        
        # Проверяем уровень уведомлений
        level = (self._level or NotificationLevel.ALL).lower()
        if level == "off":
            return False, False
        
        level_ok = True
        if level == NotificationLevel.CRITICAL_ONLY:
            level_ok = event_code in _CRITICAL_CODES
        elif level == NotificationLevel.ERRORS_ONLY:
            level_ok = event_code in _ERROR_CODES or event_code in _CRITICAL_CODES
        
        return telegram_enabled and level_ok, ntfy_enabled and level_ok
        
        return False


class _SafeDict(dict):
    """
    Позволяет форматировать строку даже если не все ключи переданы.
    Незаполненные поля заменяются на '—'.
    """
    def __missing__(self, key):
        return "—"


_notifier_instance: Optional[TelegramNotifier] = None
_notifier_lock = threading.Lock()


def get_notifier() -> TelegramNotifier:
    """Ленивая инициализация глобального TelegramNotifier без import-time side effect."""
    global _notifier_instance
    if _notifier_instance is not None:
        return _notifier_instance

    with _notifier_lock:
        if _notifier_instance is None:
            _notifier_instance = TelegramNotifier()
        return _notifier_instance


class _LazyNotifierProxy:
    """Прокси, сохраняющий старый API `from core.telegram_bot import notifier`."""

    def __getattr__(self, item):
        return getattr(get_notifier(), item)


# Глобальный ленивый прокси — используется во всём приложении
notifier = _LazyNotifierProxy()
