# API Reference: Создание стратегий для Trading Manager

Этот документ описывает контракт создания пользовательских стратегий. Каждая стратегия — это Python-модуль, размещённый в директории `strategies/`.

---

## Контракт стратегии

Движок (`LiveEngine` и `BacktestEngine`) вызывает функции стратегии в определённом порядке. Стратегия **не должна** напрямую отправлять ордера через коннектор — за это отвечает движок.

### Жизненный цикл

```
on_start() → [on_precalc() → on_bar() → execute_signal()] × N → on_stop()
```

- **Бэктест**: `on_precalc()` вызывается один раз для всей истории, затем `on_bar()` для каждого бара.
- **Live**: `on_precalc()` вызывается при обновлении истории, `on_bar()` — при каждом новом баре.

---

## Обязательные функции

### `get_info() -> dict`

Возвращает метаданные стратегии для отображения в UI.

```python
def get_info() -> dict:
    return {
        'name': 'Название стратегии',
        'version': '1.0',
        'author': 'Автор',
        'description': 'Краткое описание логики.',
        'tickers': ['SiM6'],  # Рекомендуемые инструменты
    }
```

### `get_params() -> dict`

Возвращает схему параметров для UI и оптимизации. Каждый параметр описывается словарём с полями:

| Поле | Тип | Описание |
|------|-----|----------|
| `type` | str | Тип параметра (см. ниже) |
| `default` | any | Значение по умолчанию |
| `label` | str | Отображаемое имя в UI |
| `description` | str | Подсказка |
| `min` | int/float | Минимальное значение (для int/float) |
| `max` | int/float | Максимальное значение (для int/float) |
| `options` | list | Варианты для `select` |
| `labels` | list | Отображаемые имена для `select` |

**Поддерживаемые типы:**

| Тип | Описание |
|-----|----------|
| `str` | Строка |
| `int` | Целое число |
| `float` | Дробное число |
| `bool` | Флаг (checkbox) |
| `time` | Время в формате HHMM (например, 600 = 10:00) |
| `select` | Выпадающий список |
| `ticker` | Выбор тикера |
| `instruments` | Список инструментов |
| `commission` | Комиссия (auto или число) |
| `timeframe` | Таймфрейм |

```python
def get_params() -> dict:
    return {
        'period': {
            'type': 'int',
            'default': 20,
            'min': 2,
            'max': 500,
            'label': 'Период SMA',
            'description': 'Период скользящей средней',
        },
        'order_mode': {
            'type': 'select',
            'default': 'market',
            'options': ['market', 'limit', 'limit_price'],
            'labels': ['Рыночная', 'Лимитная (стакан)', 'Лимитная (цена)'],
            'label': 'Тип заявки',
        },
    }
```

### `on_start(params: dict, connector) -> None`

Вызывается при запуске стратегии. Здесь нужно инициализировать состояние.

```python
def on_start(params: dict, connector) -> None:
    # Сброс глобального состояния
    reset_state()
    logger.info(f"[Strategy] Запуск: {params.get('ticker')}")
```

### `on_stop(params: dict, connector) -> None`

Вызывается при остановке стратегии. Здесь нужно освободить ресурсы.

```python
def on_stop(params: dict, connector) -> None:
    logger.info('[Strategy] Остановка.')
```

### `on_tick(tick_data: dict, params: dict, connector) -> None`

Вызывается при каждом тике. Для bar-based стратегий обычно не используется.

```python
def on_tick(tick_data: dict, params: dict, connector) -> None:
    pass  # Не используется в bar-based стратегиях
```

---

## Опциональные функции

### `on_precalc(df: pd.DataFrame, params: dict) -> pd.DataFrame`

Рассчитывает индикаторы для всей истории. **Используйте только векторные pandas-операции** — циклы по барам считаются ошибкой производительности.

```python
def on_precalc(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    period = int(params.get('period', 20))
    df['_sma'] = df['close'].rolling(window=period, min_periods=period).mean()
    return df
```

**Входные колонки DataFrame:**

| Колонка | Тип | Описание |
|---------|-----|----------|
| `open` | float | Цена открытия |
| `high` | float | Максимум |
| `low` | float | Минимум |
| `close` | float | Цена закрытия |
| `vol` | int | Объём |
| `date_int` | int | Дата (YYYYMMDD) |
| `time_min` | int | Время (HHMM) |
| `weekday` | int | День недели (1-7) |

**Правило:** Добавляемые колонки должны начинаться с `_` (например, `_sma`, `_rsi`).

### `on_bar(bars: list[dict], position: int, params: dict) -> dict`

Вызывается на каждом закрытом баре. Возвращает сигнал для исполнения.

```python
def on_bar(bars: list[dict], position: int, params: dict) -> dict:
    if len(bars) < 2:
        return {'action': None}
    
    current = bars[-1]
    prev = bars[-2]
    
    # Ваша логика здесь
    if should_buy(current, prev):
        return {'action': 'buy', 'qty': qty, 'comment': 'Buy signal'}
    if should_sell(current, prev):
        return {'action': 'sell', 'qty': qty, 'comment': 'Sell signal'}
    
    return {'action': None}
```

**Контракт возвращаемого значения:**

| action | Описание |
|--------|----------|
| `None` | Нет сигнала |
| `'buy'` | Открыть длинную позицию |
| `'sell'` | Открыть короткую позицию |
| `'close'` | Закрыть текущую позицию |

**Важно:**
- Не используйте реверс через `qty * 2` — сначала закройте позицию, затем откройте новую на следующем баре.
- `position` — текущая позиция (0 = нет позиции, >0 = long, <0 = short).

### `get_lookback(params: dict) -> int`

Возвращает минимальное количество баров истории, необходимое для расчёта индикаторов.

```python
def get_lookback(params: dict) -> int:
    period = int(params.get('period', 20))
    return period + 10
```

### `get_indicators() -> list`

Описывает индикаторы для отображения на графике.

```python
def get_indicators() -> list:
    return [
        {'col': '_sma', 'type': 'line', 'color': '#89b4fa', 'label': 'SMA', 'linewidth': 1.2},
        {'col': '_rsi', 'type': 'line', 'color': '#f9e2af', 'label': 'RSI', 'linewidth': 1.2},
    ]
```

**Типы индикаторов:**

| type | Описание |
|------|----------|
| `line` | Линия |
| `step` | Ступенчатая линия |
| `histogram` | Гистограмма |

### `execute_signal(signal: dict, connector, params: dict, account_id: str) -> None`

Нестандартное исполнение сигнала. Если функция не определена, движок использует стандартное исполнение.

Важно: сам факт наличия `execute_signal()` больше не включает custom path автоматически.
Стратегия должна быть зарегистрирована как explicit execution adapter в `core.strategy_loader`.

```python
def execute_signal(signal: dict, connector, params: dict, account_id: str) -> None:
    raise NotImplementedError('Custom execution допускается только для registered adapters')
```

---

## OrderPlacer — универсальный размещатель ордеров

Для стратегий с нестандартным исполнением (лимитные заявки, chase-ордеры) рекомендуется использовать `core.order_placer.OrderPlacer` вместо прямого вызова `connector.place_order()`.

### Быстрый старт

```python
from core.order_placer import OrderPlacer

def execute_signal(signal: dict, connector, params: dict, account_id: str) -> None:
    placer = OrderPlacer(connector, agent_name='MyStrategy')
    
    action = signal.get('action')
    ticker = params.get('ticker')
    board = params.get('board', 'SPBFUT')
    qty = signal.get('qty', 1)
    order_mode = params.get('order_mode', 'market')
    
    if action in ('buy', 'sell', 'close'):
        side = 'sell' if action == 'close' and position > 0 else action
        placer.place(account_id, board, ticker, side, qty, order_mode)
```

### Режимы ордеров

| order_mode | Описание |
|------------|----------|
| `market` | Рыночная заявка |
| `limit` / `limit_book` | Лимитка по лучшей цене стакана (ChaseOrder с автоперестановкой) |
| `limit_price` | Лимитка по last price с мониторингом до 23:45 |

### Методы OrderPlacer

#### `place(account_id, board, ticker, side, qty, order_mode, comment, on_filled, on_failed) -> OrderResult`

Универсальный метод размещения ордера.

#### `place_market(account_id, board, ticker, side, qty, comment) -> OrderResult`

Размещает рыночный ордер.

#### `place_chase(account_id, board, ticker, side, qty, comment, timeout, on_filled, on_failed, fallback_to_market) -> OrderResult`

Размещает chase-ордер (лимитка по стакану с автоперестановкой). Если рыночные данные недоступны — fallback на market.

#### `place_limit_price(account_id, board, ticker, side, qty, comment, on_filled, on_failed, fallback_to_market) -> OrderResult`

Размещает лимитный ордер по last price с мониторингом до TRADING_END_TIME_MIN.

#### `place_with_state(account_id, board, ticker, side, qty, order_mode, comment, on_placed, on_filled, on_failed) -> OrderResult`

Метод с callbacks для интеграции с управлением состоянием стратегии.

| Callback | Сигнатура | Когда вызывается |
|----------|-----------|------------------|
| `on_placed` | `(order_id: str) -> None` | При успешном размещении ордера |
| `on_filled` | `(filled_qty: int, avg_price: float) -> None` | При исполнении ордера |
| `on_failed` | `() -> None` | При неудаче размещения/исполнения |

### Вспомогательные функции

```python
from core.order_placer import get_last_price, get_best_bid, get_best_offer, has_market_data

# Получить последнюю цену
price = get_last_price(connector, board, ticker)

# Проверить доступность рыночных данных
if has_market_data(connector, board, ticker):
    # Можно выставлять лимитный ордер
    pass
```

---

## Best Practices

1. **Векторные операции**: В `on_precalc()` используйте только pandas-операции (`rolling`, `shift`, `groupby`, `merge`). Циклы по барам — ошибка производительности.

2. **Состояние**: Сбрасывайте глобальное состояние в `on_start()`. Если состояние меняется из нескольких потоков, защищайте его `threading.Lock`.

3. **Реверс позиции**: Не используйте `qty * 2` для реверса. Сначала верните `{'action': 'close'}`, затем на следующем баре — новый вход.

4. **Intraday vs Overnight**: Для intraday-стратегий используйте закрытие по времени (`time_min >= time_close`). Для overnight — опишите окно закрытия явно.

5. **Логирование**: Используйте `loguru.logger` для логирования. Не используйте `print()`.

6. **Обработка ошибок**: Возвращайте `{'action': None}` при невалидных данных, а не бросайте исключения.

7. **Комиссия**: Используйте тип `commission` с `default: 'auto'` для автоматического определения комиссии.

---

## Пример минимальной стратегии

```python
import pandas as pd
from loguru import logger

def get_info():
    return {'name': 'SMA Cross', 'version': '1.0', 'author': 'You', 'description': 'Пересечение SMA'}

def get_params():
    return {
        'fast_period': {'type': 'int', 'default': 10, 'min': 2, 'max': 100, 'label': 'Fast SMA'},
        'slow_period': {'type': 'int', 'default': 30, 'min': 5, 'max': 200, 'label': 'Slow SMA'},
        'qty': {'type': 'int', 'default': 1, 'min': 1, 'max': 100, 'label': 'Лотность'},
    }

def get_lookback(params):
    return int(params.get('slow_period', 30)) + 10

def on_start(params, connector):
    pass

def on_stop(params, connector):
    pass

def on_tick(tick_data, params, connector):
    pass

def on_precalc(df, params):
    fast = int(params['fast_period'])
    slow = int(params['slow_period'])
    df['_fast'] = df['close'].rolling(fast).mean()
    df['_slow'] = df['close'].rolling(slow).mean()
    return df

def on_bar(bars, position, params):
    if len(bars) < 2:
        return {'action': None}
    
    curr, prev = bars[-1], bars[-2]
    qty = int(params['qty'])
    
    # Пересечение вверх — buy, вниз — sell
    if prev['_fast'] <= prev['_slow'] and curr['_fast'] > curr['_slow']:
        return {'action': 'buy', 'qty': qty, 'comment': 'Fast > Slow'}
    if prev['_fast'] >= prev['_slow'] and curr['_fast'] < curr['_slow']:
        return {'action': 'sell', 'qty': qty, 'comment': 'Fast < Slow'}
    
    return {'action': None}
```
