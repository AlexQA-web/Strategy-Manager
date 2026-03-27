# Lingma Flow — История изменений

## Дата: 26 марта 2026

### Проблема
**Симптом:** Программа намертво зависала после планового отключения коннекторов в 00:01 и при попытке подключиться обратно в 06:55.

**Диагноз:** Блокировка механизма автореконнекта из-за некорректной работы с флагом `_stop_reconnect`.

---

## Изменения

### 1. Файл: `core/base_connector.py`

#### Строки изменены: 1-6, 142-158

#### Что changed:

**До:**
```python
from abc import ABC, abstractmethod
import threading
import time
import math
from typing import Callable, Optional


class BaseConnector(ABC):
    # ... остальной код ...
    
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
```

**После:**
```python
from abc import ABC, abstractmethod
import threading
import time
import math
from typing import Callable, Optional
from loguru import logger  # ← ДОБАВЛЕНО


class BaseConnector(ABC):
    # ... остальной код ...
    
    def start_reconnect_loop(self):
        """Запускает фоновый поток переподключения при обрыве.

        Идемпотентен: если поток уже запущен — повторный вызов игнорируется.
        Перед запуском сбрасывает флаг _stop_reconnect для корректной работы
        после планового отключения по расписанию.
        """
        # Сбрасываем флаг перед запуском — это критично для работы после disconnect()
        self._stop_reconnect.clear()  # ← ДОБАВЛЕНО
        
        if hasattr(self, "_reconnect_thread") and self._reconnect_thread.is_alive():
            logger.debug(f"[{self.__class__.__name__}] reconnect-loop уже запущен")  # ← ДОБАВЛЕНО
            return
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True, name="reconnect-loop"
        )
        self._reconnect_thread.start()
```

#### Причина изменений:

**Проблема:** После вызова `disconnect()` (в 00:01 по расписанию), метод устанавливал `self._stop_reconnect.set()`, что останавливало поток `_reconnect_loop`. Когда планировщик пытался подключить коннектор повторно (в 06:55), метод `start_reconnect_loop()` проверял только активность потока, но **не сбрасывал флаг `_stop_reconnect`**. В результате:
- Новый поток не мог стартовать (флаг всё ещё установлен)
- Коннектор не мог переподключиться
- Программа зависала

**Решение:** Явно сбрасывать `_stop_reconnect.clear()` перед запуском reconnect_loop. Это гарантирует, что механизм автореконнекта будет работать корректно после планового отключения.

---

### 2. Файл: `core/quik_connector.py`

#### Строки изменены: 98-111

#### Что changed:

**До:**
```python
def is_connected(self) -> bool:
    """Проверяет состояние подключения к QUIK.
    
    Использует кэшированное состояние _connected, но также проверяет что
    _client существует и _stop_event не установлен (соединение не в процессе отключения).
    Реальная проверка соединения (ping) выполняется в фоновом потоке reconnect_loop.
    """
    # Проверяем не только флаг, но и что клиент жив и не в процессе отключения
    if not self._connected or self._client is None:
        return False
    # Дополнительная проверка: если идёт отключение - не считаем подключённым
    if self._stop_reconnect.is_set():  # ← ПРОБЛЕМА
        return False
    return True
```

**После:**
```python
def is_connected(self) -> bool:
    """Проверяет состояние подключения к QUIK.
    
    Использует кэшированное состояние _connected, но также проверяет что
    _client существует. Реальная проверка соединения (ping) выполняется
    в фоновом потоке reconnect_loop.
    """
    # Проверяем не только флаг, но и что клиент жив
    if not self._connected or self._client is None:
        return False
    return True  # ← УБРАНА ЛИШНЯЯ ПРОВЕРКА
```

#### Причина изменений:

**Проблема:** После ночного отключения (00:01) флаг `_stop_reconnect` оставался установленным. Утром (06:55) даже если `connect()` успешно подключался к QUIK, метод `is_connected()` возвращал `False` потому что проверял `_stop_reconnect.is_set()`. Это блокировало запуск LiveEngine и стратегий.

**Решение:** Убрать проверку `_stop_reconnect.is_set()` из `is_connected()`. Флаг `_stop_reconnect` предназначен **только** для управления циклом reconnet_loop, а не для определения текущего состояния подключения. Реальная проверка соединения выполняется методом `ping()` в фоновом потоке.

---

## Обоснование архитектурных решений

### Почему `_stop_reconnect` не должен использоваться в `is_connected()`?

1. **Разделение ответственности:**
   - `_stop_reconnect` — управляющий флаг для **цикла переподключения**
   - `_connected` — состояние **текущего соединения**
   
2. **Жизненный цикл:**
   - `disconnect()` → `_stop_reconnect.set()` → **останавливает reconnect_loop**
   - `connect()` → `_stop_reconnect.clear()` → **запускает reconnect_loop**
   
3. **Консистентность:**
   - `FinamConnector.is_connected()` просто возвращает `self._connected`
   - `QuikConnector.is_connected()` теперь тоже следует этому паттерну

### Почему сброс флага в `start_reconnect_loop()` безопасен?

1. **Идемпотентность:** Метод уже проверяет `if self._reconnect_thread.is_alive()` перед запуском
2. **Синхронизация:** Сброс происходит **до** проверки, что исключает гонки
3. **Логика:** Если мы вызываем `start_reconnect_loop()`, значит хотим чтобы reconnet работал

---

## Тестирование

### Сценарий для проверки:
1. Дождаться планового отключения в 00:01
2. В 06:55 проверить автоматическое подключение коннекторов
3. Убедиться что GUI не зависает
4. Проверить что стратегии запускаются

### Ожидаемое поведение:
- ✅ Коннекторы подключаются в 06:55 без ошибок
- ✅ `is_connected()` возвращает `True` сразу после подключения
- ✅ Reconnect loop работает корректно
- ✅ Стратегии запускаются автоматически

---

## Дополнительные улучшения

### Импорт `loguru.logger` в `base_connector.py`

Добавлен явный импорт `logger` для консистентности с другими модулями и чтобы избежать импорта внутри методов (`from loguru import logger` в `_fire()` и `_reconnect_loop()`).

---

## Ссылки на файлы

- Исходный файл: [`core/base_connector.py`](core/base_connector.py)
- Исходный файл: [`core/quik_connector.py`](core/quik_connector.py)
