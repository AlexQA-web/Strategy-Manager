import requests
import threading
import time
from typing import Optional
from loguru import logger

from core.storage import get_setting


class NtfyNotifier:
    """
    Класс для отправки уведомлений через NTFY.
    """

    def __init__(self):
        self._server_url: Optional[str] = None
        self._topic: Optional[str] = None
        self._enabled: bool = False

    def configure(self, server_url: str, topic: str, enabled: bool = True):
        """
        Настраивает NTFY уведомления.
        
        Args:
            server_url: URL сервера NTFY (например, https://ntfy.sh)
            topic: Название топика для отправки уведомлений
            enabled: глобальный флаг включения
        """
        self._server_url = server_url.strip().rstrip('/') if server_url else None
        self._topic = topic.strip() if topic else None
        
        # Проверяем, что все необходимые параметры заданы и разрешены
        if enabled and self._server_url and self._topic:
            self._enabled = True
            logger.info(f"NTFY настроен. Сервер: {self._server_url}, топик: {self._topic}")
        else:
            self._enabled = False
            logger.warning("NTFY выключен или не настроен: отсутствует URL/топик или disabled")

    def load_from_settings(self):
        """
        Загружает конфигурацию из settings.json.
        """
        server_url = get_setting("ntfy_server_url")
        topic = get_setting("ntfy_topic")
        enabled = str(get_setting("ntfy_enabled", "false")).lower() == "true"

        if server_url and topic and enabled:
            self.configure(server_url, topic, enabled=True)
        else:
            self._enabled = False
            logger.info("NTFY выключен или не настроен (нет server_url/topic или disabled)")

    def send(self, message: str, title: str = "Trading Manager", priority: str = "default", tags: list = None) -> bool:
        """
        Отправляет уведомление через NTFY.
        
        Args:
            message: Текст уведомления
            title: Заголовок уведомления
            priority: Приоритет уведомления (min, low, default, high, emergency)
            tags: Список тегов для улучшения визуализации
        
        Returns:
            bool: True если уведомление отправлено успешно, иначе False
        """
        if not self._enabled:
            return False

        url = f"{self._server_url}/{self._topic}"

        headers = {
            "Title": title,
            "Priority": priority,
            "Content-Type": "text/plain; charset=utf-8",
        }

        if tags:
            headers["Tags"] = ",".join(tags)

        try:
            # Явно передаем строку и позволяем requests обработать кодировку
            response = requests.post(url, data=message.encode('utf-8'), headers=headers, timeout=10)
            
            if response.status_code in [200, 202]:
                logger.debug(f"NTFY уведомление отправлено: {title}")
                return True
            else:
                logger.error(f"Ошибка отправки NTFY уведомления: {response.status_code} - {response.text}")
                return False
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка сети при отправке NTFY уведомления: {e}")
            return False
        except Exception as e:
            logger.error(f"Неизвестная ошибка при отправке NTFY уведомления: {e}")
            return False

    def test_connection(self) -> tuple[bool, str]:
        """
        Проверяет соединение с NTFY.
        
        Returns:
            tuple[bool, str]: (успех: bool, сообщение: str)
        """
        if not self._enabled:
            return False, "NTFY не настроен"

        test_msg = f"✅ Тест соединения NTFY успешен\nВремя: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        success = self.send(test_msg, title="Тест NTFY", priority="low", tags=["test"])
        
        if success:
            return True, f"Подключено к {self._server_url}, топик: {self._topic}"
        else:
            return False, f"Ошибка отправки тестового сообщения"


# Глобальный экземпляр
_ntfy_notifier_instance: Optional[NtfyNotifier] = None
_ntfy_notifier_lock = threading.Lock()


def get_ntfy_notifier() -> NtfyNotifier:
    """
    Ленивая инициализация глобального NtfyNotifier.
    """
    global _ntfy_notifier_instance
    if _ntfy_notifier_instance is not None:
        return _ntfy_notifier_instance

    with _ntfy_notifier_lock:
        if _ntfy_notifier_instance is None:
            _ntfy_notifier_instance = NtfyNotifier()
        return _ntfy_notifier_instance


class _LazyNtfyNotifierProxy:
    """
    Прокси, позволяющий использовать старый API.
    """
    
    def __getattr__(self, item):
        return getattr(get_ntfy_notifier(), item)


# Глобальный ленивый прокси — используется во всём приложении
ntfy_notifier = _LazyNtfyNotifierProxy()