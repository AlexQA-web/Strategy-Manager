"""
Dependency Injection Container — простой DI-контейнер для управления зависимостями.

Позволяет регистрировать и разрешать зависимости по интерфейсу,
поддерживает синглтоны и фабрики.
"""

import threading
from typing import Any, Callable, Dict, Optional, Type, TypeVar
from loguru import logger


T = TypeVar('T')


class DIContainer:
    """
    Простой DI-контейнер.
    
    Пример использования:
        container = DIContainer()
        container.register(CommissionManager, lambda: CommissionManager())
        manager = container.resolve(CommissionManager)
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._services: Dict[type, Any] = {}
        self._factories: Dict[type, Callable] = {}
        self._singletons: Dict[type, bool] = {}
        self._instances: Dict[type, Any] = {}

    def register(
        self,
        interface: Type[T],
        impl: Any = None,
        *,
        singleton: bool = True,
        factory: Optional[Callable] = None,
    ) -> None:
        """
        Зарегистрировать зависимость.
        
        Args:
            interface: Тип интерфейса (ключ для resolve)
            impl: Экземпляр или класс реализации
            singleton: Если True — возвращать один и тот же экземпляр
            factory: Фабрика для создания экземпляра (альтернатива impl)
        """
        if factory is not None:
            self._factories[interface] = factory
            self._singletons[interface] = singleton
            logger.debug(f'[DIContainer] Зарегистрирована фабрика для {interface.__name__}')
        elif impl is not None:
            if isinstance(impl, type):
                # impl — класс, создаём через factory
                self._factories[interface] = lambda: impl()
                self._singletons[interface] = singleton
                logger.debug(f'[DIContainer] Зарегистрирован класс {impl.__name__} для {interface.__name__}')
            else:
                # impl — экземпляр
                self._services[interface] = impl
                self._instances[interface] = impl
                logger.debug(f'[DIContainer] Зарегистрирован экземпляр для {interface.__name__}')
        else:
            raise ValueError('Нужно указать impl или factory')

    def resolve(self, interface: Type[T]) -> T:
        """
        Получить зарегистрированную зависимость.
        
        Args:
            interface: Тип интерфейса
            
        Returns:
            Экземпляр реализации
            
        Raises:
            KeyError: если зависимость не зарегистрирована
        """
        with self._lock:
            # Сначала проверяем синглтон-экземпляры
            if interface in self._instances:
                return self._instances[interface]

            # Проверяем прямые сервисы
            if interface in self._services:
                return self._services[interface]

            # Создаём через фабрику
            if interface in self._factories:
                instance = self._factories[interface]()
                if self._singletons.get(interface, True):
                    self._instances[interface] = instance
                logger.debug(f'[DIContainer] Создан экземпляр для {interface.__name__}')
                return instance

            raise KeyError(f'Зависимость не зарегистрирована: {interface.__name__}')

    def resolve_optional(self, interface: Type[T]) -> Optional[T]:
        """Получить зависимость или None если не зарегистрирована."""
        try:
            return self.resolve(interface)
        except KeyError:
            return None

    def has(self, interface: Type[T]) -> bool:
        """Проверить, зарегистрирована ли зависимость."""
        return (
            interface in self._services
            or interface in self._factories
            or interface in self._instances
        )

    def clear(self) -> None:
        """Очистить все регистрации."""
        self._services.clear()
        self._factories.clear()
        self._singletons.clear()
        self._instances.clear()
        logger.info('[DIContainer] Контейнер очищен')

    def __repr__(self) -> str:
        services = set(self._services.keys()) | set(self._factories.keys()) | set(self._instances.keys())
        names = [cls.__name__ for cls in services]
        return f'DIContainer({names})'


# Глобальный контейнер для использования в приложении
container = DIContainer()
