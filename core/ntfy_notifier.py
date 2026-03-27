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

    def configure(self, server_url: str, topic: str):
        """
        Настраивает NTFY уведомления.
        
        Args:
            server_url: URL сервера NTFY (например, https://ntfy.sh)
            topic: Название топика для отправки уведомлений
        """
        self._server_url = server_url.strip().rstrip('/')
        self._topic = topic.strip()
        
        # Проверяем, что все необходимые параметры заданы
        if self._server_url and self._topic:
            self._enabled = True
            logger.info(f"NTFY настроен. Сервер: {self._server_url}, топик: {self._topic}")
        else:
            logger.warning("NTFY не настроен: отсутствует URL сервера или название топика")

    def load_from_settings(self):
        """
        Загружает конфигурацию из settings.json.
        """
        server_url = get_setting("ntfy_server_url")
        topic = get_setting("ntfy_topic")

        if server_url and topic:
            self.configure(server_url, topic)
        else:
            logger.info("NTFY не настроен (нет server_url или topic в settings.json)")

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