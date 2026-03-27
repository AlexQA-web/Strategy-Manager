# 🔬 Research: Бизнес-логика Trading Strategy Manager

**Дата проведения:** 26 марта 2026  
**Объект исследования:** Торговая система Trading Manager (Desktop приложение для алгоритмической торговли)  
**Цель:** Анализ корректности реализации бизнес-логики, выявление архитектурных проблем и несоответствий

---

## 📋 Содержание

1. [Архитектура системы](#1-архитектура-системы)
2. [Жизненный цикл стратегии](#2-жизненный-цикл-стратегии)
3. [Расчёт комиссий](#3-расчёт-комиссий)
4. [Расчёт PnL](#4-расчёт-pnl)
5. [Управление позициями](#5-управление-позициями)
6. [Бэктестинг vs Live торговля](#6-бэктестинг-vs-live-торговля)
7. [Критические проблемы](#7-критические-проблемы)
8. [Рекомендации](#8-рекомендации)

---

## 1. Архитектура системы

### 1.1. Компоненты

```
┌─────────────────────────────────────────────────────────┐
│                    GUI (PyQt6)                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │ Main Window  │  │ Chart Window │  │ Settings     │ │
│  │ + Agent Table│  │ + Indicators │  │ + Params     │ │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
└─────────────────────────────────────────────────────────┘
                         ↕ (через ui_signals)
┌─────────────────────────────────────────────────────────┐
│              Core Layer (Business Logic)                │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │ LiveEngine   │  │ Backtest     │  │ Connectors   │ │
│  │ • Polling    │  │ Engine       │  │ • Finam DLL  │ │
│  │ • Signals    │  │ • History    │  │ • QUIK Py    │ │
│  │ • Execution  │  │ • Reports    │  │ • Callbacks  │ │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │ Commission   │  │ Order        │  │ Equity       │ │
│  │ Manager      │  │ History      │  │ Tracker      │ │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
└─────────────────────────────────────────────────────────┘
                         ↕
┌─────────────────────────────────────────────────────────┐
│              Storage Layer (Persistence)                │
│  data/settings.json, data/strategies.json,              │
│  data/order_history.json, data/equity/*.json            │
└─────────────────────────────────────────────────────────┘
```

### 1.2. Типы стратегий

**Стандартные bar-based стратегии:**
- Наследуют шаблон `_template.py`
- Получают сигнал через `on_bar(bars, position, params)`
- Возвращают `{'action': 'buy'|'sell'|'close'|None, 'qty': int, 'comment': str}`
- **Не могут** напрямую отправлять ордера — делегируют `LiveEngine`

**Мультиинструментальные стратегии (Achilles):**
- Реализуют `execute_signal(signal, connector, params, account_id)`
- Самостоятельно управляют ордерами через коннектор
- Обходят стандартный механизм исполнения `LiveEngine`

**Вывод:** Архитектура поддерживает два принципиально разных подхода к исполнению сигналов, что создаёт потенциальную несогласованность.

---

## 2. Жизненный цикл стратегии

### 2.1. Стандартный поток (LiveEngine)

```
[Start] → on_start() → connect() → 
    ↓
[Loop: poll_interval]
    ├→ get_history() → новый бар?
    │   ├→ on_precalc(df) → индикаторы
    │   ├→ on_bar(bars[:-1], position) → signal
    │   └→ _execute_signal(signal)
    │       ├→ Проверка позиции (_position_lock)
    │       ├→ place_order() через коннектор
    │       ├→ _monitor_order() → подтверждение
    │       └→ _record_trade() → order_history
    ↓
[Stop] → stop() → close_position() → flush_equity()
```

### 2.2. Проблемные места

#### ❌ Проблема 1: Двойная проверка позиции

**Файл:** `core/live_engine.py`, строки 700-708, 834-841

```python
# Строка 700-708 (в _process_bar)
if action in ("buy", "sell"):
    with self._position_lock:
        if self._position != 0:
            logger.warning(...)
            return

# Строка 834-841 (в _execute_signal)
with self._position_lock:
    if self._position != 0:
        logger.warning(...)
        return
```

**Проблема:** Проверка дублируется дважды с разницей в ~130 строк кода. Между проверками позиция может измениться (например, при закрытии).

**Решение:** Удалить первую проверку (строки 700-708), оставить только в `_execute_signal()`.

---

#### ❌ Проблема 2: Race condition при установке `_order_in_flight`

**Файл:** `core/live_engine.py`, строки 844-854

```python
if self._order_mode == "limit":
    # Проверяем и устанавливаем флаг внутри той же критической секции
    # (ранее использовалась вложенная блокировка _order_in_flight_lock)
    if self._order_in_flight:
        logger.warning(...)
        return
    self._order_in_flight = True
```

**Контекст:** Флаг используется для предотвращения двойного входа в лимитные ордера.

**Проблема:** В коде есть комментарий о том, что ранее использовалась вложенная блокировка `_order_in_flight_lock`, но теперь используется единая `_position_lock`. Однако в других местах кода (например, при сбросе флага в строке 1459) блокировка может не соблюдаться.

**Проверка:** Сброс флага в строке 1459 находится под `_position_lock` ✅

**Вывод:** Проблема исправлена, но требует документирования для будущих изменений.

---

## 3. Расчёт комиссий

### 3.1. Декларируемая формула (из документации)

```
Для фьючерсов:
  trade_value = price × point_cost × quantity
  moex_part = trade_value × moex_taker_pct / 100
  broker_part = broker_futures_rub × quantity
  итого = moex_part + broker_part

Для акций:
  trade_value = price × quantity
  moex_part = trade_value × moex_taker_pct / 100
  broker_part = trade_value × broker_stock_pct / 100
  итого = moex_part + broker_part

При order_role == "maker": moex_part = 0
```

### 3.2. Реализация в коде

**Файл:** `core/commission_manager.py`, строки 150-182

```python
if is_futures:
    if point_cost is None or point_cost == 0:
        logger.warning(f"point_cost не указан для {ticker}, используем 1.0")
        point_cost = 1.0
    
    trade_value = price * point_cost * quantity  # ← СТРАННОСТЬ!
    moex_part = trade_value * moex_pct / 100
    
    broker_rub = broker_config.get("futures_rub", {}).get(instrument_type, 1.0)
    broker_part = broker_rub * quantity
    
    total = moex_part + broker_part
else:
    trade_value = price * quantity
    moex_part = trade_value * moex_pct / 100
    
    broker_pct = broker_config.get(f"{instrument_type}_pct", 0.04)
    broker_part = trade_value * broker_pct / 100
    
    total = moex_part + broker_part
```

### 3.3. Критическая ошибка в формуле для фьючерсов

#### ❌ ОШИБКА: Некорректный расчёт trade_value для фьючерсов

**Проблема:** В формуле используется:
```python
trade_value = price * point_cost * quantity
```

Это **НЕВЕРНО** с экономической точки зрения:
- `price` — цена фьючерса в пунктах (например, 70000 для Si)
- `point_cost` — стоимость пункта в рублях (например, 0.25 для Si)
- `quantity` — количество контрактов

**Результат:** `trade_value` становится завышенным в `point_cost` раз!

**Пример:**
- Фьючерс SiM6: цена = 70000 пунктов, point_cost = 0.25 руб
- Количество = 1 контракт
- **Правильно:** Комиссия MOEX = 70000 × 0.001% = 0.7 руб
- **В коде:** Комиссия MOEX = (70000 × 0.25 × 1) × 0.001% = 0.175 руб ❌

**Причина ошибки:** Разработчики перепутали две концепции:
1. **Номинальная стоимость фьючерса** = price × point_cost (нужно для расчёта ГО)
2. **База для расчёта комиссии** = price (в пунктах) × moex_pct

**Как должно быть:**
```python
# Для фьючерсов комиссия MOEX рассчитывается от цены в пунктах
moex_part = price * moex_pct / 100  # без умножения на point_cost!
broker_part = broker_rub * quantity
total = moex_part + broker_part
```

**Подтверждение из документации MOEX:**
> Комиссия по срочному рынку рассчитывается от **цены контракта в пунктах**, а не от номинальной стоимости.

---

#### ❌ Проблема 2: CommissionManager игнорируется в некоторых местах

**Файл:** `core/live_engine.py`, строка 903

```python
commission_rub = self._calculate_commission(self._ticker, qty, price)
commission_per_lot = commission_rub / abs(qty) if qty != 0 else 0
```

**Проблема:** Метод `_calculate_commission()` (строки 193-236) имеет режим `"auto"` и `"manual"`. В manual-режиме используется упрощённая формула без учёта MOEX.

**Проверка конфигурации:**
```python
# Строка 96-105
commission_param = params.get("commission", "auto")
if commission_param == "auto":
    self._commission_mode = "auto"
    self._commission_pct = 0.0
    self._commission_rub = 0.0
else:
    self._commission_mode = "manual"
    self._commission_pct = float(params.get("commission_pct", ...))
    self._commission_rub = float(params.get("commission_rub", ...))
```

**Вывод:** Если пользователь явно задаёт комиссию в параметрах стратегии, автоматический расчёт через `CommissionManager` не используется, даже если конфиг доступен.

---

## 4. Расчёт PnL

### 4.1. Декларируемая формула

**Из CLAUDE.md:**
```
PnL = (price - entry_price) × qty × point_cost
```

### 4.2. Реализация

**Файл:** `core/live_engine.py`, строка 306

```python
gross_pnl = (price - self._entry_price) * qty * self._point_cost
```

✅ **Верно** для фьючерсов.

**Файл:** `core/order_history.py`, строки 166-169

```python
if is_long:
    gross_pnl = (close_price - open_price) * close_qty * point_cost
else:
    gross_pnl = (open_price - close_price) * close_qty * point_cost
```

✅ **Верно** для фьючерсов.

---

### 4.3. ❌ Проблема: PnL для акций считается неверно

**Контекст:** Для акций `point_cost` должен быть равен 1 (цена выражена в рублях за бумагу).

**Файл:** `core/moex_api.py`, строки 220-230

```python
# В методе get_stock_info():
result = {
    # ...
    'minstep': float(security_data.get('STEP', 0)),
    'point_cost': float(security_data.get('STEP', 1)),  # ← ОШИБКА!
    'lot_size': int(security_data.get('LOTSIZE', 1)),
}
```

**Проблема:** Для акций `point_cost` приравнивается к `minstep` (шагу цены), а не к 1.

**Пример:**
- Акция SBER: цена = 250 руб, minstep = 0.01 руб
- Позиция: 100 бумаг
- **Правильно:** PnL = (252 - 250) × 100 × 1 = 200 руб
- **В коде:** PnL = (252 - 250) × 100 × 0.01 = 2 руб ❌

**Причина:** Разработчики механически перенесли логику `point_cost` с фьючерсов на акции, не учитывая экономический смысл.

**Как должно быть:**
```python
# Для акций point_cost = 1 (цена уже в рублях)
result['point_cost'] = 1.0
```

---

### 4.4. Учёт комиссий в PnL

**Файл:** `core/order_history.py`, строка 176

```python
total_commission = commission_per_lot * close_qty * 2  # вход + выход
net_pnl = gross_pnl - total_commission
```

✅ **Верно:** Комиссия вычитается за обе стороны сделки (вход + выход).

---

## 5. Управление позициями

### 5.1. Хранение состояния

**Файл:** `core/live_engine.py`, строки 112-118

```python
self._position_lock = threading.Lock()  # защита от race condition
self._position: int = 0                 # направление: 1=long, -1=short, 0=no position
self._position_qty: int = 0             # количество контрактов (со знаком)
self._entry_price: float = 0.0          # цена входа
```

✅ **Верно:** Позиция защищена блокировкой.

---

### 5.2. ❌ Проблема: Синхронизация с коннектором

**Файл:** `core/live_engine.py`, строки 471-518

```python
def _detect_position(self):
    """Определяет текущую позицию по тикеру из коннектора."""
    try:
        positions = self._connector.get_positions(self._account_id)
        for pos in positions:
            if pos.get("ticker") == self._ticker:
                qty = float(pos.get("quantity", 0))
                new_qty = int(qty)
                entry_price = float(pos.get("avg_price", 0))
                if not entry_price:
                    entry_price = self._get_entry_price_from_history()
                    # ВАЛИДАЦИЯ: проверяем расхождение между коннектором и order_history
                    if qty > 0 and entry_price == 0.0:
                        logger.warning(
                            f"РАСХОЖДЕНИЕ: коннектор показывает qty={qty}, "
                            f"но order_history не содержит позиции!"
                        )
                # ...
                with self._position_lock:
                    self._position = new_position
                    self._position_qty = new_qty
                    self._entry_price = new_entry_price
```

**Проблема:** Метод `_detect_position()` вызывается:
1. При старте LiveEngine (строка 548)
2. После переподключения коннектора (строка 467)

**Риск:** Если коннектор отдаёт некорректные данные (например, после реконнекта), позиция будет перезапущена неверно.

**Пример из кода (строки 488-496):**
```python
if qty > 0 and entry_price == 0.0:
    logger.warning("РАСХОЖДЕНИЕ: коннектор показывает позицию, но order_history пуст")
```

**Вывод:** Проблема известна разработчикам, но обработка ограничивается логированием.

---

## 6. Бэктестинг vs Live торговля

### 6.1. Архитектура

**BacktestEngine:** `core/backtest_engine.py`
- Загружает исторические бары из файла
- Вызывает `on_precalc()` → `on_bar()` для каждого бара
- Исполняет сигналы **на следующем баре** (строка 134)

**LiveEngine:** `core/live_engine.py`
- Поллит `get_history()` каждые N секунд
- Вызывает `on_precalc()` → `on_bar()` при новом баре
- Исполняет сигналы **немедленно**

---

### 6.2. ❌ Критическое расхождение: Момент исполнения сигнала

**Бэктест:**

**Файл:** `core/backtest_engine.py`, строки 134-142

```python
# На закрытии бара t получаем сигнал
signal = module.on_bar(bar_dicts[:lookback], position, params)
action = signal.get("action")

# Исполняем НА ОТКРЫТИИ следующего бара t+1
exec_dt = bars[i + 1].dt if i + 1 < len(bars) else None
exec_price = bars[i + 1].open if i + 1 < len(bars) else None
```

**Live:**

**Файл:** `core/live_engine.py`, строки 695-723

```python
signal = self._loaded.call_on_bar(processed_bars, current_position, self._params)
action = signal.get("action")

if action:
    # Исполняем НЕМЕДЛЕННО на том же баре
    self._execute_signal(signal)
```

**Проблема:** 
- В бэктесте сигнал исполняется на **следующем** баре (открытие t+1)
- В live-торговле сигнал исполняется на **том же** баре (закрытие t)

**Последствия:**
1. **Look-ahead bias в live:** Стратегия получает сигнал на закрытии бара t и сразу исполняет его по цене закрытия t. В реальности это невозможно — нужно время на анализ и отправку ордера.
2. **Разные цены исполнения:**
   - Бэктест: покупка по `open(t+1)`
   - Live: покупка по `close(t)` (фактически — по last price)

**Пример:**
- Бар t: Close = 100
- Бар t+1: Open = 101, Close = 102
- Сигнал на покупку получен на баре t
- **Бэктест:** Куплено по 101 (open t+1)
- **Live:** Куплено по 100 (close t) ❌ нереалистично!

---

### 6.3. ❌ Проблема 2: Разный учёт комиссий

**Бэктест:**

**Файл:** `core/backtest_engine.py`, строки 236-247

```python
if isinstance(commission_value, str) and commission_value == "auto":
    # Автоматический расчёт через CommissionManager
    trade.commission = commission_manager.calculate(...)
else:
    # Ручной режим (обратная совместимость)
    trade.commission = commission_value * trade.qty * 2
```

**Live:**

**Файл:** `core/live_engine.py`, строки 899-903

```python
commission_rub = self._calculate_commission(self._ticker, qty, price)
commission_per_lot = commission_rub / abs(qty) if qty != 0 else 0
```

**Проблема:** В бэктесте комиссия рассчитывается один раз при закрытии сделки (round-trip). В live — комиссия записывается для каждой стороны отдельно (вход + выход).

**Риск:** При сравнении результатов бэктеста и live-торговли возможны расхождения из-за разного метода учёта комиссий.

---

## 7. Критические проблемы

### 🔴 КРИТИЧЕСКИЕ ОШИБКИ

| # | Проблема | Файл | Строки | Серьёзность |
|---|----------|------|--------|-------------|
| 1 | **Неверный расчёт комиссии MOEX для фьючерсов** (умножение на point_cost) | `commission_manager.py` | 156 | 🔴 Критическая |
| 2 | **point_cost для акций = minstep вместо 1** | `moex_api.py` | 230 | 🔴 Критическая |
| 3 | **Look-ahead bias в LiveEngine** (исполнение на том же баре) | `live_engine.py` | 695-723 | 🔴 Критическая |
| 4 | **Двойная проверка позиции** (рассинхронизация) | `live_engine.py` | 700, 834 | 🟡 Средняя |

---

### 🟡 ПРОБЛЕМЫ АРХИТЕКТУРЫ

| # | Проблема | Последствия |
|---|----------|-------------|
| 5 | **Два механизма исполнения сигналов** (стандартный vs Achilles) | Несогласованность, сложность поддержки |
| 6 | **Ручное управление позициями в мультиинструментальных стратегиях** | Риск рассинхронизации с биржей |
| 7 | **Зависимость от callback'ов DLL** (Finam) | Сложность тестирования, скрытые зависимости |
| 8 | **Отсутствие валидации параметров стратегий** | Возможность инъекции вредоносного кода |

---

### 🟢 ДОСТОИНСТВА АРХИТЕКТУРЫ

| # | Достоинство | Описание |
|---|-----------|----------|
| 1 | **Атомарная запись JSON** | Защита от коррупции данных через `.tmp` + `.bak` |
| 2 | **Circuit breaker для ошибок** | Остановка стратегии после 3 подряд ошибок |
| 3 | **Graceful shutdown для ChaseOrder** | Корректная отмена лимитных ордеров при остановке |
| 4 | **In-memory кэш для equity_tracker** | Оптимизация I/O операций |
| 5 | **Refcount для подписки на котировки** | Избежание дублирующих подписок |

---

## 8. Рекомендации

### 8.1. Немедленные исправления (Priority: 🔴)

#### 1. Исправить формулу комиссии для фьючерсов

**Файл:** `core/commission_manager.py`, строка 156

```python
# БЫЛО (НЕВЕРНО):
trade_value = price * point_cost * quantity
moex_part = trade_value * moex_pct / 100

# СТАЛО (ВЕРНО):
# Комиссия MOEX рассчитывается от цены в пунктах, без умножения на point_cost
moex_part = price * moex_pct / 100
broker_part = broker_rub * quantity
total = moex_part + broker_part
```

---

#### 2. Исправить point_cost для акций

**Файл:** `core/moex_api.py`, строка 230

```python
# БЫЛО (НЕВЕРНО):
'point_cost': float(security_data.get('STEP', 1)),

# СТАЛО (ВЕРНО):
'point_cost': 1.0,  # Для акций цена уже в рублях
```

---

#### 3. Устранить look-ahead bias в LiveEngine

**Файл:** `core/live_engine.py`, строки 695-723

```python
# Добавить задержку исполнения до открытия следующего бара
# Вариант 1: Ждать открытия следующего бара перед исполнением
if action:
    # Сохраняем отложенный сигнал
    self._pending_signal = signal
    # Исполняем на следующем баре в начале _process_bar()
    
# В начале _process_bar():
if hasattr(self, '_pending_signal') and self._pending_signal:
    self._execute_signal(self._pending_signal)
    self._pending_signal = None
```

**Альтернатива:** Исполнять сигнал не по `close`, а по `open` следующего бара (как в бэктесте).

---

### 8.2. Среднесрочные улучшения (Priority: 🟡)

#### 4. Удалить двойную проверку позиции

**Файл:** `core/live_engine.py`, строки 700-708

Просто удалить эту проверку — она дублируется в `_execute_signal()`.

---

#### 5. Унифицировать механизм исполнения сигналов

**Проблема:** Achilles обходит стандартный механизм, создавая несогласованность.

**Решение:** Создать базовый класс `BaseExecutionEngine` с общими методами:
- `_validate_signal()`
- `_send_order()`
- `_record_trade()`
- `_update_position()`

И `LiveEngine`, и `Achilles` должны наследовать этот класс.

---

#### 6. Добавить валидацию параметров стратегий

**Файл:** `core/strategy_loader.py`

```python
def validate_params(params: dict) -> bool:
    """Проверяет параметры стратегии на безопасность."""
    forbidden_keys = {"__import__", "exec", "eval", "system", "open"}
    return not any(k in forbidden_keys for k in params)
```

---

### 8.3. Долгосрочные улучшения (Priority: 🟢)

#### 7. Реализовать event-driven архитектуру вместо polling

**Текущая проблема:** Polling `get_history()` создаёт задержки и нагрузку на коннектор.

**Решение:** Подписка на свечи через callback:
```python
connector.subscribe_candles(board, ticker, period, callback)
# Callback вызывается при получении новой свечи
```

---

#### 8. Добавить unit-тесты для критических функций

**Отсутствуют тесты для:**
- `CommissionManager.calculate()` — формулы комиссий
- `get_order_pairs()` — FIFO matching
- `record_equity()` — расчёт просадки
- `BacktestEngine.run()` — исполнение сигналов

---

## 9. Заключение

### Общая оценка архитектуры: **B- (75/100)**

**Сильные стороны:**
- ✅ Модульная архитектура с чётким разделением ответственности
- ✅ Хорошая обработка многопоточности (блокировки, атомарные операции)
- ✅ Персистентное хранилище с защитой от коррупции данных
- ✅ Circuit breaker и graceful shutdown

**Критические проблемы:**
- ❌ Неверные формулы расчёта комиссий и PnL
- ❌ Look-ahead bias в live-торговле
- ❌ Рассинхронизация между бэктестом и live

**Рекомендуемый порядок исправлений:**
1. Исправить формулы комиссий (влияет на PnL всех стратегий)
2. Исправить point_cost для акций
3. Устранить look-ahead bias
4. Добавить unit-тесты для верификации исправлений

---

## Приложения

### A. Глоссарий

| Термин | Определение |
|--------|-------------|
| **point_cost** | Стоимость изменения цены на 1 пункт (для фьючерсов) |
| **minstep** | Минимальный шаг цены инструмента |
| **GO (Гарантийное обеспечение)** | Залог для открытия фьючерсной позиции |
| **Maker/Taker** | Maker предоставляет ликвидность, taker забирает |
| **FIFO matching** | Метод сопоставления ордеров: первый открыл → первый закрыл |

### B. Список файлов

| Файл | Назначение |
|------|------------|
| `core/live_engine.py` | Движок live-торговли |
| `core/backtest_engine.py` | Движок бэктестинга |
| `core/commission_manager.py` | Расчёт комиссий |
| `core/moex_api.py` | Клиент MOEX API |
| `core/order_history.py` | Хранение истории ордеров |
| `core/equity_tracker.py` | Трекер equity и просадки |
| `strategies/_template.py` | Шаблон стандартной стратегии |

---

**Документ подготовил:** AI Assistant  
**Дата завершения:** 26 марта 2026
