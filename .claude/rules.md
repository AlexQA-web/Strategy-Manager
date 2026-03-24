# Правила разработки — Trading Strategy Manager

## 1. Язык и стиль кода

- Весь код пишется на **Python 3.11+**
- Комментарии, docstring, сообщения логов — на **русском языке**
- Имена переменных, функций, классов, файлов — на **английском языке**
- Type hints обязательны для публичных методов и функций
- Строки — одинарные кавычки `'...'`, f-строки для форматирования
- Максимальная длина строки — 120 символов

## 2. Логирование

- Использовать **только loguru**: `from loguru import logger`
- Стандартная библиотека `logging` — только в модулях без loguru-зависимостей (moex_api.py, commission_settings_widget.py)
- Уровни:
  - `logger.debug()` — детали потока выполнения, значения переменных
  - `logger.info()` — значимые события (подключение, запуск стратегии, исполнение ордера)
  - `logger.warning()` — нештатные, но обрабатываемые ситуации
  - `logger.error()` — ошибки с потерей данных или сбоем операции
- Формат: `f"[ClassName] Описание: {detail}"` — всегда указывать контекст
- **Не использовать** `print()` нигде в продакшн-коде

## 3. Потокобезопасность

- Все операции с разделяемым состоянием — под `threading.Lock()`
- Никогда не захватывать несколько lock'ов одновременно (deadlock)
- GUI-обновления — **только из главного потока Qt** через сигналы `ui_signals.*`
- Фоновые задачи — `threading.Thread(daemon=True)`
- Для блокирующих вызовов в GUI (get_history, get_sec_info) — выносить в daemon-поток с таймаутом
- `_position_lock` в LiveEngine — единственный lock для состояния позиции; не вкладывать другие lock'и внутрь

## 4. Обработка ошибок

- Всегда оборачивать внешние вызовы (DLL, сеть, файл) в `try/except`
- Не глотать исключения молча — минимум `logger.warning(f"... error: {e}")`
- При ошибках коннектора возвращать `None` или `False`, не бросать исключения наружу
- `circuit_breaker` в LiveEngine — не обходить, не отключать
- В стратегиях все исключения из `on_bar` / `execute_signal` должны быть пойманы внутри; стратегия не должна крашить LiveEngine

## 5. Работа с данными (storage.py)

- Для чтения/записи JSON — **только** `read_json()` / `write_json()` из `core/storage.py`
- Для атомарного read-modify-write одной настройки — `save_setting()` (захватывает `_write_lock`)
- Не обращаться к файлам `data/*.json` напрямую через `open()` в других модулях
- `order_history.json` — только через функции `core/order_history.py`
- `trades_history.json` — только через `append_trade()` / `get_trades()` из `storage.py`
- Не хранить критичные данные только в памяти — сбрасывать на диск при каждой сделке (`force_flush=True`)

## 6. Коннекторы

- Новый коннектор — наследник `BaseConnector` из `core/base_connector.py`
- Обязательно реализовать все `@abstractmethod`
- Регистрировать в `register_connectors()` (в `core/connector_manager.py`), **не** при импорте модуля
- ID коннектора — строка в нижнем регистре: `"finam"`, `"quik"`
- `get_history()` должен возвращать DataFrame с колонками `Open, High, Low, Close, Volume` и `DatetimeIndex`
- `get_sec_info()` должен возвращать dict с ключами `point_cost`, `minstep`, `buy_deposit`, `sell_deposit`
- `subscribe_quotes()` / `unsubscribe_quotes()` — идемпотентны (refcount внутри)
- `is_connected()` — быстрый (не блокирующий) вызов; реальная проверка — в reconnect loop

## 7. Стратегии

### Обязательный интерфейс
```python
def get_info() -> dict
def get_params() -> dict
def on_start(params: dict, connector) -> None
def on_stop(params: dict, connector) -> None
def on_tick(tick_data: dict, params: dict, connector) -> None
```

### Опциональный интерфейс (баровые стратегии)
```python
def on_precalc(df: pd.DataFrame, params: dict) -> pd.DataFrame  # O(n), pandas only
def on_bar(bars: list[dict], position: int, params: dict) -> dict
def get_lookback(params: dict) -> int
def execute_signal(signal: dict, connector, params: dict, account_id: str) -> None
def get_indicators() -> list[dict]
```

### Правила для стратегий
- `on_precalc` — только pandas-операции, никаких Python-циклов по барам (O(n), не O(n²))
- `on_bar` — только логика сигнала, никаких вызовов брокера, никаких side effects
- Стратегия не должна хранить состояние между вызовами `on_bar` в глобальных переменных без сброса в `on_start`
- Для мультиинструментальных стратегий — реализовывать `execute_signal` (не `on_bar`)
- Глобальное состояние в стратегии (если нужно) — сбрасывать вызовом `reset_state()` из `on_start`
- Добавлять `_EXCLUDE_DATES` для известных праздников/нерабочих дней

### Параметры стратегий
- Все параметры описаны в `get_params()` с полями: `type`, `default`, `label`, `description`
- Числовые параметры — с `min`, `max`, `step`
- Комиссию объявлять с типом `"commission"` и `default: "auto"`
- Тикер объявлять с типом `"ticker"`, борд — через TickerParamWidget (не отдельным полем)
- Тип `"time"` — значение в минутах от полуночи (0–1439)

## 8. LiveEngine

- Не добавлять блокирующие вызовы в `_poll_loop` — только через фоновый поток с таймаутом
- `_position_lock` захватывается атомарно для чтения и изменения позиции вместе
- `_order_in_flight` устанавливается/снимается **внутри** `_position_lock`
- После каждой сделки — вызов `record_equity(..., force_flush=True)`
- Circuit breaker (`_consecutive_failures`) — не обходить даже в тестах
- При добавлении нового `order_mode` — реализовать мониторинг в фоновом потоке (не блокировать poll)

## 9. UI (PyQt6)

- Цветовая тема — **Catppuccin Mocha**, не менять основные цвета:
  - bg: `#1e1e2e`, surface: `#181825`, overlay: `#313244`
  - text: `#cdd6f4`, blue: `#89b4fa`, green: `#a6e3a1`, red: `#f38ba8`
- Все QSpinBox / QDoubleSpinBox / QComboBox в скролл-областях — наследовать с `wheelEvent` игнором без фокуса
- Новый тип параметра стратегии → создать класс-наследник `BaseParamWidget` → зарегистрировать в `ParamWidgetFactory.register()`
- Диалоги с длинным контентом — оборачивать в `QScrollArea`
- Межпоточные обновления — **только** через сигналы (`ui_signals.*`), не через прямой вызов методов виджетов из потока
- `QTimer.singleShot(0, callback)` — для отложенного вызова в GUI-потоке из фонового

## 10. Расчёт комиссий

- Всегда использовать `commission_manager.calculate()` в режиме `"auto"`
- Не хардкодить ставки в стратегиях или LiveEngine
- `point_cost` для фьючерсов: сначала MOEX API, fallback — данные коннектора
- Формулы:
  - Фьючерсы: `price × point_cost × qty × moex_pct% + broker_rub × qty`
  - Акции: `price × qty × (moex_pct + broker_pct)%`
- Комиссия в `make_order()` — в руб/лот (одна сторона), НЕ суммарная

## 11. MOEX API (core/moex_api.py)

- Использовать кэш: фьючерсы TTL=4 часа, акции TTL=24 часа
- Не делать HTTP-запросы в GUI-потоке
- При ошибке — возвращать `None`, не бросать исключение
- `get_instrument_info(ticker, sec_type)` — единственная точка входа

## 12. Новый функционал — чеклист

При добавлении нового модуля:
- [ ] Написать логирование с контекстом `[ClassName]`
- [ ] Добавить обработку ошибок во всех публичных методах
- [ ] Если используется разделяемое состояние — добавить lock
- [ ] Если это синглтон — создать глобальный экземпляр в конце файла
- [ ] Обновить `CLAUDE.md` (структура проекта)

При добавлении нового коннектора:
- [ ] Наследовать `BaseConnector`
- [ ] Реализовать все abstract methods
- [ ] Добавить в `register_connectors()`
- [ ] Добавить UI-блок в `settings_window.py` (`_tab_<name>()`)
- [ ] Добавить расписание в `schedules.json` defaults

При добавлении новой стратегии:
- [ ] Реализовать все 5 обязательных функций
- [ ] Описать все параметры в `get_params()` с `type`, `default`, `label`, `description`
- [ ] Проверить совместимость с BacktestEngine (корректный `on_bar`)
- [ ] Добавить `get_lookback()` если используется история
- [ ] Сбрасывать состояние в `on_start()`

## 13. Запрещено

- ❌ `print()` в продакшн-коде
- ❌ `time.sleep()` в GUI-потоке
- ❌ Прямые вызовы методов виджетов из фоновых потоков
- ❌ Вложенные lock'и (deadlock)
- ❌ Хранение паролей/токенов в коде (только в `data/settings.json`)
- ❌ Импорт `ui.*` из `core.*` (нарушение слоёв: core → ui запрещено)
- ❌ Импорт модулей стратегий напрямую (только через `strategy_loader`)
- ❌ Удаление/очистка `data/order_history.json` без явного запроса пользователя
- ❌ Хардкод путей файлов вне `config/settings.py`
- ❌ Блокирующие сетевые вызовы без таймаута
- ❌ `O(n²)` алгоритмы в `on_precalc` — только pandas vectorized операции
- ❌ Изменение `_position_qty` без захвата `_position_lock`
- ❌ Регистрация коннекторов при импорте модуля (только в `register_connectors()`)

## 14. Проверка кода перед коммитом

```bash
# Синтаксис
python -m py_compile core/<file>.py
python -m py_compile ui/<file>.py
python -m py_compile strategies/<file>.py

# Или через venv
.venv/Scripts/python.exe -m py_compile <file>.py
```

Запускать после каждого изменения файла. Не коммитить с синтаксическими ошибками.
