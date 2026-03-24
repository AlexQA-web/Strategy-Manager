# strategies/ — контекст для Claude Code

Общие правила проекта: см. корневой `CLAUDE.md` и `@../.claude/rules.md`.

## Шаблон

Для новой стратегии — копировать `_template.py`.
Актуальный пример стандартной bar-based стратегии — `example_strategy.py`.

## Базовая идея контракта

В проекте основной сценарий для обычной стратегии — bar-based:

- `LiveEngine` загружает историю, вызывает `on_precalc()`, затем `on_bar()` на каждом новом закрытом баре;
- `BacktestEngine` также работает через `on_precalc() -> on_bar()`;
- сама стратегия обычно **не** отправляет заявки напрямую через `connector`;
- стратегия возвращает только сигнал, а стандартное исполнение делает движок;
- если нужен reverse, делается `close` на текущем баре и только потом новый вход на следующем баре;
- reverse через `qty * 2` для обычных стратегий не использовать.

## Обязательный интерфейс загрузчика

Это минимальный набор, который сейчас валидируется в `core/strategy_loader.py`:

```python
def get_info() -> dict
def get_params() -> dict
def on_start(params, connector) -> None
def on_stop(params, connector) -> None
def on_tick(tick_data, params, connector) -> None
```

Важно:

- `on_tick()` для обычной bar-based стратегии чаще всего пустой (`pass`), но функция должна существовать;
- отсутствие этих функций ломает загрузку стратегии ещё до запуска;
- bar-based логика при этом живёт не в `on_tick()`, а в `on_precalc()` и `on_bar()`.

## Рекомендуемый bar-based интерфейс

Для обычной стратегии в этом проекте фактически ожидается такой набор:

```python
def on_precalc(df, params) -> pd.DataFrame
def on_bar(bars, position, params) -> dict
def get_lookback(params) -> int
```

Дополнительно можно определить:

```python
def get_indicators() -> list[dict]
def execute_signal(signal, connector, params, account_id) -> None
```

Правила:

- `on_precalc()` считает индикаторы по всей истории и возвращает DataFrame;
- `on_bar()` принимает список баров с уже рассчитанными полями и возвращает торговый сигнал;
- `get_lookback()` должен возвращать разумный запас истории в барах для расчёта индикаторов;
- `get_indicators()` нужен только для отображения линий/гистограмм на графике;
- `execute_signal()` добавляется только для special-case исполнения, а не по умолчанию.

## `on_bar()` → формат возврата

```python
{'action': None}
{'action': 'buy', 'qty': int, 'comment': str}
{'action': 'sell', 'qty': int, 'comment': str}
{'action': 'close', 'qty': int, 'comment': str}
```

Опционально стратегия может вернуть цену для специальных режимов исполнения:

```python
{'action': 'buy', 'qty': 1, 'comment': 'Long signal', 'price': 85000.0}
```

Минимально рекомендуется всегда заполнять:

- `action`
- `qty`
- `comment`

## Правило reverse: только close-first

Если стратегия получила противоположный сигнал при уже открытой позиции:

```python
if position == 1 and crossed_down:
    return {'action': 'close', 'qty': qty, 'comment': 'Close long before possible short'}

if position == -1 and crossed_up:
    return {'action': 'close', 'qty': qty, 'comment': 'Close short before possible long'}
```

Не делать так:

```python
return {'action': 'sell', 'qty': qty * 2, 'comment': 'Reverse'}
```

Сначала закрытие, потом новый вход на следующем баре.

## Временная логика: intraday и overnight

Не существует одного универсального шаблона для всех стратегий.

### Intraday-стратегии

Обычно:

- новые входы разрешены только внутри окна торговли;
- открытая позиция закрывается по времени через условие вида `time_min >= time_close`.

Пример:

```python
if position != 0 and time_min >= time_close:
    return {'action': 'close', 'qty': qty, 'comment': f'Close by time {time_min}'}

if not (time_open <= time_min < time_close):
    return {'action': None}
```

### Overnight-стратегии

Если позицию можно переносить через границу сессии, окно закрытия нужно описывать явно.
Нельзя слепо копировать intraday-условие `time_min >= time_close`.

Пример overnight-окна:

```python
if position != 0 and time_close <= time_min < time_open:
    return {'action': 'close', 'qty': qty, 'comment': 'Overnight close window'}
```

Итог: временная логика должна соответствовать фактической модели стратегии, а не шаблону по умолчанию.

## `on_precalc()` — критичные правила

Предпочтительно использовать pandas-операции над всей историей:

```python
# ✅ Нормально: vectorized / rolling / groupby / merge
df['_sma'] = df['close'].rolling(window=period, min_periods=period).mean()

daily = df.groupby('date_int').agg(high=('high', 'max'), low=('low', 'min'))
daily['_prev_high'] = daily['high'].shift(1)
df = df.merge(daily[['_prev_high']], left_on='date_int', right_index=True, how='left')
```

Чего избегать:

```python
# ❌ Плохо: тяжёлый Python-цикл по всей истории, особенно с df.loc/df.iloc внутри
for i in range(len(df)):
    df.loc[i, '_sma'] = df['close'].iloc[max(0, i - period):i].mean()
```

Уточнение:

- цель правила — не допускать тяжёлые `O(n²)`-паттерны на полной истории;
- небольшой локальный цикл допустим, если он ограничен, прозрачен и действительно нужен;
- если ту же логику можно выразить через `rolling`, `shift`, `groupby`, `merge`, нужно предпочитать pandas-вариант.

## Проверка `NaN` в `on_bar()`

```python
val = current.get('_indicator')
if val is None or val != val:
    return {'action': None}
```

Либо отдельным helper:

```python
def _bad(value) -> bool:
    if value is None:
        return True
    try:
        return value != value
    except Exception:
        return True
```

## Глобальное состояние

Если стратегии нужно модульное состояние, объявлять его явно и сбрасывать при старте.

```python
_my_state: dict = {}


def reset_state() -> None:
    global _my_state
    _my_state = {}


def on_start(params, connector) -> None:
    reset_state()
```

Правила:

- состояние не должно «переживать» перезапуск стратегии;
- если состояние меняется из нескольких потоков, нужен lock;
- обычной bar-based стратегии глобальное состояние часто вообще не требуется.

## `get_params()` — типы параметров

```python
'ticker':      {'type': 'ticker',     'default': 'SiM6',   'label': '...', 'description': '...'}
'qty':         {'type': 'int',        'default': 1,        'min': 1, 'max': 1000, ...}
'time_open':   {'type': 'time',       'default': 600}      # минуты от полуночи
'time_close':  {'type': 'time',       'default': 1425}
'order_mode':  {'type': 'select',     'default': 'market', 'options': [...], 'labels': [...]}
'commission':  {'type': 'commission', 'default': 'auto'}
'instruments': {'type': 'instruments','default': [...]}    # только если стратегия реально мультиинструментальная
```

Замечания:

- для обычной стратегии комиссия обычно остаётся `auto`;
- `order_mode` имеет смысл, если стратегия использует стандартное исполнение движка;
- `instruments` не нужен в обычной одноинструментальной стратегии.

## `get_indicators()` — формат

```python
[
    {'col': '_sma',   'type': 'line', 'color': '#89b4fa', 'label': 'SMA', 'linewidth': 1.2},
    {'col': '_upper', 'type': 'line', 'color': '#a6e3a1', 'label': 'Upper', 'linestyle': '--'},
    {'col': '_hist',  'type': 'step', 'color': '#f38ba8', 'label': 'Hist'},
]
```

Правила:

- колонка должна реально существовать после `on_precalc()`;
- служебные расчёты, которые не нужны на графике, лучше не публиковать;
- обычно индикаторные колонки в стратегиях называют с префиксом `_`.

## `execute_signal()` — только для special-case исполнения

По умолчанию `LiveEngine` сам исполняет сигнал, который вернул `on_bar()`.

`execute_signal()` добавлять только если стандартного исполнения недостаточно, например:

- мультиинструментальная стратегия;
- собственный lifecycle лимитных заявок;
- особая логика подтверждения позиции или закрытия;
- стратегия, которая осознанно берёт реальное исполнение на себя.

Обычной bar-based стратегии `execute_signal()` обычно не нужен.

Базовый каркас:

```python
def execute_signal(signal, connector, params, account_id):
    action = signal.get('action')
    strategy_id = params.get('_strategy_id', '')
    connector_id = params.get('_connector_id', '')
    # ...
```

## Запись сделок из `execute_signal()`

Если стратегия обходит стандартное исполнение движка и исполняет сделки сама, она обязана сама же корректно писать историю сделок.

```python
from core.order_history import make_order, save_order
from core.commission_manager import commission_manager

comm = commission_manager.calculate(
    ticker=ticker,
    board=board,
    quantity=qty,
    price=price,
    order_role='taker',
    point_cost=1.0,
    connector_id=connector_id,
)
order = make_order(
    strategy_id,
    ticker,
    side,
    qty,
    price,
    board,
    comment=comment,
    commission=comm / qty,
    point_cost=1.0,
)
save_order(order)
```

## Исключённые даты

```python
_EXCLUDE_DATES = {220224, 220225}   # формат YYMMDD (int)


def on_bar(bars, position, params):
    if bars[-1]['date_int'] in _EXCLUDE_DATES:
        return {'action': None}
```

## Текущие стратегии

| Файл | Тип | Инструмент | Особенность |
|------|-----|------------|-------------|
| `_template.py` | bar-based | любой | канонический шаблон |
| `example_strategy.py` | bar-based | один инструмент | пример SMA crossover без мгновенного reverse |
| `achilles.py` | bar-based | корзина акций | special-case `execute_signal()`, `_state_lock` |
| `bochka_cny.py` | bar-based | фьючерс CNY | special-case `execute_signal()`, подтверждённая позиция, окно закрытия |
| `daytrend.py` | bar-based | фьючерсы Si/RI | дневной диапазон, overnight удержание допустимо |
| `tracker.py` | bar-based | фьючерсы/акции | SMA±ATR, сжатие TF в `on_precalc()` |
| `valera_trend.py` | bar-based | фьючерсы | SMA-тренд, `candle_count`, overnight окно закрытия |
