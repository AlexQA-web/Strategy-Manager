# core/connector_manager.py

import threading
from typing import Optional
from loguru import logger

from core.base_connector import BaseConnector
from core.storage import get_setting


class ConnectorManager:
    """
    Динамический реестр коннекторов. Каждый коннектор живёт в своём потоке
    и не знает о существовании другого.
    """

    def __init__(self):
        self._registry: dict[str, BaseConnector] = {}
        self._lock = threading.Lock()

    def register(self, connector_id: str, connector: BaseConnector):
        """Регистрирует коннектор в реестре."""
        with self._lock:
            if connector_id in self._registry:
                logger.warning(f"[ConnectorManager] Коннектор '{connector_id}' уже зарегистрирован, перезаписываем")
            self._registry[connector_id] = connector
            logger.info(f"[ConnectorManager] Зарегистрирован коннектор: {connector_id}")

    def unregister(self, connector_id: str) -> bool:
        """Удаляет коннектор из реестра."""
        with self._lock:
            if connector_id not in self._registry:
                logger.warning(f"[ConnectorManager] Коннектор '{connector_id}' не найден")
                return False
            del self._registry[connector_id]
            logger.info(f"[ConnectorManager] Коннектор '{connector_id}' удалён из реестра")
            return True

    def get(self, connector_id: str) -> Optional[BaseConnector]:
        with self._lock:
            c = self._registry.get(connector_id)
            if not c:
                logger.debug(f"[ConnectorManager] Коннектор не найден: {connector_id} (доступные: {list(self._registry.keys())})")
            return c

    def all(self) -> dict[str, BaseConnector]:
        with self._lock:
            return dict(self._registry)

    def configure_all(self):
        """Применяет настройки реконнекта из settings.json ко всем коннекторам."""
        attempts = int(get_setting("reconnect_attempts") or 5)
        delay    = int(get_setting("reconnect_delay")    or 5)
        with self._lock:
            connectors = list(self._registry.values())
        for connector in connectors:
            connector.configure_reconnect(attempts, delay)

    def start_reconnect_loops(self):
        """Запускает петли авторекконекта для всех коннекторов."""
        with self._lock:
            items = list(self._registry.items())
        for cid, connector in items:
            logger.info(f"[ConnectorManager] Запуск reconnect-loop для {cid}")
            connector.start_reconnect_loop()

    def is_any_connected(self) -> bool:
        with self._lock:
            connectors = list(self._registry.values())
        return any(c.is_connected() for c in connectors)

    def status(self) -> dict[str, bool]:
        with self._lock:
            items = list(self._registry.items())
        return {cid: c.is_connected() for cid, c in items}


connector_manager = ConnectorManager()

# ── Отложенная регистрация встроенных коннекторов ───────────────────────────────
# NOTE: Регистрация коннекторов перенесена в явный вызов register_connectors()
# для возможности юнит-тестирования без реального окружения.
# Вызывать после настройки приложения.

def register_connectors():
    """Регистрирует встроенные коннекторы. Вызывается после инициализации UI.
    
    Всегда регистрирует finam и quik.
    """
    from core.finam_connector import finam_connector
    from core.quik_connector import quik_connector
    
    connector_manager.register("finam", finam_connector)
    connector_manager.register("quik", quik_connector)
    
    # Автоматическая инициализация реконнекта при загрузке модуля
    connector_manager.configure_all()
