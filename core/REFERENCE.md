# core/ — контекст для Claude Code

Общие правила проекта: см. корневой `CLAUDE.md` и `@../.claude/rules.md`.

## Архитектурный принцип

`core/` — не знает о `ui/`. Импорт `ui.*` из `core.*` запрещён.
Стратегии загружаются только через `strategy_loader`, не напрямую.

## Синглтоны (один экземпляр на приложение)

```python
# Создаются в конце каждого модуля:
connector_manager    = ConnectorManager()     # core/connector_manager.py
commission_manager   = CommissionManager()    # core/commission_manager.py
instrument_classifier = InstrumentClassifier() # core/instrument_classifier.py
position_manager     = PositionManager()      # core/position_manager.py
strategy_loader      = StrategyLoader()       # core/strategy_loader.py
strategy_scheduler   = StrategyScheduler()    # core/scheduler.py
notifier             = TelegramNotifier()     # core/telegram_bot.py
finam_connector      = FinamConnector()       # core/finam_connector.py
quik_connector       = QuikConnector()        # core/quik_connector.py
```

> Не создавать дополнительные экземпляры. Не регистрировать коннекторы при импорте модуля — только в `register_connectors()`.

## storage.py — правила

```python
# Чтение/запись произвольного JSON:
from core.storage import read_json, write_json

# Одна настройка (атомарно, под _write_lock):
from core.storage import get_setting, save_setting

# Read-modify-write нескольких ключей атомарно:
from core.storage import _write_lock, _read, _write_unsafe, SETTINGS_FILE
with _write_lock:
    data = _read(SETTINGS_FILE, use_cache=False)  # use_cache=False обязательно!
    data["key1"] = v1
    data["key2"] = v2
    _write_unsafe(SETTINGS_FILE, data)

# ЗАПРЕЩЕНО: прямой open() для файлов data/*.json в других модулях
```

## order_history.py — API

```python
from core.order_history import make_order, save_order, get_orders, get_order_pairs, get_total_pnl

# commission: руб/лот, ОДНА сторона (не round-trip!)
order = make_order(strategy_id, ticker, side, qty, price, board,
                   comment="", commission=2.5, point_cost=10.0)
save_order(order)

# PnL пар:
pairs = get_order_pairs(strategy_id)   # FIFO matching
total = get_total_pnl(strategy_id)     # float | None
```

## equity_tracker.py — API

```python
from core.equity_tracker import record_equity, get_max_drawdown, flush_all

record_equity(strategy_id, equity=realized+unrealized, position_qty=qty)
record_equity(strategy_id, equity, position_qty, force_flush=True)  # после сделки
flush_all()  # при остановке приложения
```

## commission_manager.py — API

```python
from core.commission_manager import commission_manager

# Расчёт комиссии за одну сторону:
comm = commission_manager.calculate(
    ticker="SiM6", board="FUT", quantity=1, price=85000.0,
    order_role="taker",   # "taker" | "maker"
    point_cost=10.0,      # для фьючерсов
    connector_id="finam", # "finam" | "quik"
)

# Детализация:
breakdown = commission_manager.get_breakdown(...)
```

## connector_manager.py — API

```python
from core.connector_manager import connector_manager

connector = connector_manager.get("finam")    # → BaseConnector | None
connector = connector_manager.get("quik")
all_connectors = connector_manager.all()      # dict[str, BaseConnector]
connector_manager.is_any_connected()          # bool
connector_manager.status()                    # dict[str, bool]
```

## BaseConnector — контракт

При добавлении нового коннектора реализовать все методы:

```python
connect() -> bool
disconnect()
is_connected() -> bool                          # быстрый, не блокирующий
place_order(account_id, ticker, side, quantity, order_type, price, board, agent_name) -> str|None
cancel_order(order_id, account_id) -> bool
get_positions(account_id) -> list[dict]
get_accounts() -> list[dict]
get_last_price(ticker, board) -> float|None
get_order_book(board, ticker, depth=10) -> dict|None  # {"bids":[(p,v)], "asks":[(p,v)]}
close_position(account_id, ticker, quantity=0, agent_name="") -> bool
get_history(ticker, board, period, days) -> pd.DataFrame|None
get_free_money(account_id) -> float|None
get_sec_info(ticker, board) -> dict|None        # point_cost, minstep, buy_deposit, sell_deposit
subscribe_quotes(board, ticker)                 # idempotent, refcount внутри
unsubscribe_quotes(board, ticker)
get_best_quote(board, ticker) -> dict|None      # {"bid":f, "offer":f, "last":f}
```

**`get_history` контракт:**
- Колонки: `Open, High, Low, Close, Volume` (заглавные)
- Индекс: `DatetimeIndex`, отсортирован по возрастанию
- `period`: строка `"1m"` `"5m"` `"15m"` `"30m"` `"1h"` `"4h"` `"1d"`

**`get_sec_info` контракт:**
```python
{
    "point_cost":   float,  # руб/пункт (MOEX API приоритет над DLL)
    "minstep":      float,  # мин. шаг цены
    "buy_deposit":  float,  # ГО покупателя
    "sell_deposit": float,  # ГО продавца
    "lotsize":      int,    # размер лота
}
```

## live_engine.py — критичные правила

**`_position_lock`** — единственный lock для состояния позиции:
- `_position`, `_position_qty`, `_entry_price`, `_order_in_flight` — только под этим lock'ом
- Не вкладывать `_chase_lock` внутрь `_position_lock` (deadlock)
- Проверка + установка `_order_in_flight` — в одной `with _position_lock:` секции

**Circuit breaker:**
- `_consecutive_failures >= 3` → `self.stop()`
- Не обходить даже в отладке

**get_history таймаут:**
- Вызывается в фоновом потоке с `join(timeout=30)`
- Если завис → пропускаем тик, не блокируем poll_loop

## chase_order.py — правила

```python
# Порядок при отмене — НЕЛЬЗЯ менять:
cancel_order(tid, account_id)
_wait_for_terminal_status(tid, timeout=2.0)   # ждём fills после cancel
unwatch_order(tid, watcher)                   # только потом отписываемся
```

## moex_api.py — кэш

```python
from core.moex_api import MOEXClient

info = MOEXClient.get_instrument_info(ticker, sec_type="futures")
# sec_type: "futures" | "stock"
# Кэш: фьючерсы 4ч, акции 24ч
# При ошибке → None (не бросает исключение)
```

## instrument_classifier.py — приоритет классификации

```
manual_mapping[ticker] → prefix_rules[prefix] → board (FUT→futures, TQBR→stock)
```

```python
from core.instrument_classifier import instrument_classifier

itype = instrument_classifier.classify("SiM6", "FUT")  # "currency_futures"
is_fut = instrument_classifier.is_futures("SBER", "TQBR")  # False
```

## telegram_bot.py — отправка событий

```python
from core.telegram_bot import notifier, EventCode

notifier.send(EventCode.ORDER_FILLED,
              agent="my_strategy",
              description="BUY SiM6 x1 @ 85000")
```

## strategy_loader.py — circuit breaker

`LoadedStrategy` считает последовательные ошибки в `on_bar`:
- `>= 5` ошибок подряд → `state = StrategyState.ERROR`
- `call_on_bar()` возвращает `{"action": None}` в состоянии ERROR
- Сброс: `loaded.reset_error()`
