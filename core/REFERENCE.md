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

# Новые после рефакторинга:
valuation_service    = ValuationService()     # core/valuation_service.py
fill_ledger          = FillLedger()           # core/fill_ledger.py
reservation_ledger   = ReservationLedger()    # core/reservation_ledger.py
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

## valuation_service.py — единый денежный калькулятор

```python
from core.valuation_service import valuation_service

# PnL multiplier: фьючерсы → point_cost, акции → lot_size
mult = valuation_service.get_pnl_multiplier(is_futures=True, point_cost=13.7, lot_size=1)

# Unrealized PnL (открытая позиция):
pnl = valuation_service.compute_open_pnl(
    entry_price=85000, current_price=86000, qty=2,
    pnl_multiplier=1.0, entry_commission=1.9, exit_commission=1.9,
)

# Realized PnL (одна FIFO-пара):
pnl = valuation_service.compute_closed_pnl(
    open_price=100, close_price=120, qty=1,
    is_long=True, pnl_multiplier=10.0,
    entry_commission=1.3, exit_commission=1.3,
)

# Equity snapshot:
eq = valuation_service.compute_equity_snapshot(
    realized_pnl=500, entry_price=85000, current_price=86000,
    position_qty=2, pnl_multiplier=1.0,
)

# Пропорциональное деление комиссии при partial FIFO:
c = valuation_service.slice_commission(total_commission=50.0, slice_qty=4, source_qty=10)
```

> **Не дублировать** денежные формулы в других модулях. Все PnL/commission/equity — через ValuationService.

## fill_ledger.py — canonical fill source

```python
from core.fill_ledger import fill_ledger

# Записать fill (дедупликация по fill_id):
ok = fill_ledger.record_fill(
    fill_id="exec_001",        # уникальный execution_id от коннектора
    strategy_id="my_strategy",
    ticker="SBER", board="TQBR", side="buy",
    qty=10, price=300.0,
    agent_name="my_agent",
    commission_total=6.45,
    pnl_multiplier=10.0,
)
# ok=True → записан, ok=False → дубликат или пустой fill_id

# Проверка дубликата:
fill_ledger.is_duplicate("exec_001")  # True
```

> **Не записывать** fills напрямую через `save_order` / `append_trade` — только через FillLedger.

## reservation_ledger.py — резервирование buying power

```python
from core.reservation_ledger import reservation_ledger

# Зарезервировать перед submit ордера:
reservation_ledger.reserve("strat_A:order_1", "account_123", 50000.0)

# Доступные средства с учётом резервов:
avail = reservation_ledger.available("account_123", gross_free=100000.0)  # 50000.0

# Освободить при fill/cancel/reject:
reservation_ledger.release("strat_A:order_1")

# Stale eviction: резервы старше 5 мин автоматически удаляются
```

## position_tracker.py — матрица переходов

```
Разрешённые (trade path):
    flat  → long     open_position("buy", qty, price)
    flat  → short    open_position("sell", qty, price)
    long  → flat     close_position(filled, total_qty)
    short → flat     close_position(filled, total_qty)
    long  → long*    close_position (partial — уменьшение qty)
    short → short*   close_position (partial — уменьшение qty)

Запрещённые (trade path):
    long  → short    flip (confirm_open возвращает False)
    short → long     flip (confirm_open возвращает False)
    long  → long+    scale-in (confirm_open возвращает False)
    short → short+   scale-in (confirm_open возвращает False)

Sync path (update_position) — не ограничен, для reconcile с брокером.
```

## risk_guard.py — pre-trade risk + circuit breaker

```python
from core.risk_guard import RiskGuard

rg = RiskGuard(
    strategy_id="my_strat",
    circuit_breaker_threshold=3,    # N ошибок → circuit open
    circuit_breaker_timeout=60.0,   # сброс счётчика через N сек
    max_position_size=10,           # макс. qty в одном ордере
    daily_loss_limit=5000.0,        # дневной лимит убытка (абс. руб.)
    get_total_pnl=get_total_pnl,    # callback для realized PnL
)

# Pre-trade check (вызывается в OrderExecutor перед submit):
allowed, reason = rg.check_risk_limits("buy", qty=5)

# Circuit breaker (hard-stop, только close разрешён):
rg.is_circuit_open()       # True → запретить buy/sell
rg.record_failure()        # ошибка → счётчик +1
rg.record_success()        # успех → сброс счётчика
rg.reset_circuit_breaker() # ручной сброс
```

## live_engine.py — sync_status и degraded state

```
sync_status:
    unknown   → начальное состояние до первого reconcile
    synced    → позиция подтверждена брокером
    stale     → данные брокера устарели (>N сек без подтверждения)
    degraded  → систематический отказ reconcile

В degraded state:
    - Открывающие ордера (buy/sell) ЗАБЛОКИРОВАНЫ
    - Close и reconcile РАЗРЕШЕНЫ
    - Торговля возобновляется только после успешного resync → synced
```

## Canonical order execution path

```
on_bar() → signal
  → LiveEngine._process_bar()
    → RiskGuard.is_circuit_open()          # hard-stop check
    → RiskGuard.check_risk_limits()        # position size + daily loss
    → OrderExecutor._check_account_risk_limits()  # gross exposure + positions count
    → ReservationLedger.reserve()          # reserve buying power
    → connector.place_order()              # submit to exchange
    → OrderExecutor._monitor_*()           # poll order status
      → FillLedger.record_fill()           # canonical fill → order_history + trades_history
      → PositionTracker.confirm_open() / close_position()
      → ReservationLedger.release()        # release buying power
      → TradeRecorder._flush_equity()      # via ValuationService
```

> **Не обходить** этот путь из стратегий. Custom `execute_signal()` допускается только для зарегистрированных strategy adapters.
