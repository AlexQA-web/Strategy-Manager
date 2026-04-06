# ПОЛНЫЙ АУДИТ ТОРГОВОЙ СИСТЕМЫ Trading Strategy Manager

**Дата:** 2026-04-06  
**Аудитор:** Principal-level trading systems engineer  
**Версия системы:** текущий `main`  
**Контекст:** система работает с реальными деньгами на Московской бирже

---

## 🎯 ОЦЕНКА НАДЁЖНОСТИ: 7.0 / 10

| Аспект | Оценка | Комментарий |
|--------|--------|-------------|
| Архитектура | 8/10 | Чёткое разделение слоёв, правильный DI, хорошая модульность |
| Потокобезопасность | 6/10 | Основные пути защищены, но есть TOCTOU в FillLedger и пробелы в risk_guard |
| Управление ордерами | 7/10 | Submission blocking + order_in_flight, но chase fallback не проверяет partial fills |
| Reconciliation | 7/10 | Периодическая сверка работает, self-heal неразрушающий, но history_qty возвращает 0 при ошибке |
| PnL / Комиссии | 8/10 | Формулы корректны, ValuationService использует Decimal, FIFO-пары правильно |
| Lifecycle | 8/10 | **Нет автоматического закрытия позиций** ✅, graceful stop, watchdog |
| Отказоустойчивость | 6/10 | Нет retry для emergency close, stale резервы не удаляются |
| Тестирование | 7/10 | 34 файла, 500+ тестов, но пробелы в UI и QuikConnector |
| Безопасность | 8/10 | DPAPI для секретов, RWLock для storage, но DPAPI fallback на plaintext |

**Статусы по сводной таблице:**
- **Архитектура — СОГЛАСЕН.** Общая layered-структура и DI-границы действительно выглядят здраво по текущему коду.
- **Потокобезопасность — СОГЛАСЕН.** Оценка в целом соответствует дереву: базовые lock-механизмы есть, но несколько race действительно подтверждаются.
- **Управление ордерами — ЧАСТИЧНО СОГЛАСЕН.** Общая оценка разумна, но тезис про chase fallback в summary жёстче, чем текущая реализация конкретной fallback-ветки.
- **Reconciliation — СОГЛАСЕН.** Summary корректно отражает наличие working reconcile-path и проблему с `history_qty == 0` на exception.
- **PnL / Комиссии — СОГЛАСЕН.** Финансовый блок в целом сильный, а главные замечания касаются precision/performance, а не базовых формул.
- **Lifecycle — СОГЛАСЕН.** Отсутствие автоматического закрытия позиций действительно подтверждается.
- **Отказоустойчивость — СОГЛАСЕН.** Отсутствие retry для emergency close и проблема stale reservations реальны.
- **Тестирование — ЧАСТИЧНО СОГЛАСЕН.** Пробелы в UI и QUIK есть, но конкретные числа в summary я отдельно не перепроверял полностью.
- **Безопасность — ЧАСТИЧНО СОГЛАСЕН.** DPAPI и RWLock используются, но подпункт про `plaintext fallback` в текущем коде не подтверждается.

---

## 🔥 ТОП-10 РИСКОВ (ранжированы по потенциальному ущербу)

### РИСК 1: FillLedger TOCTOU — дубликат fill может пройти
**Файл:** [core/fill_ledger.py](core/fill_ledger.py) ~L127-182  
**Severity:** 🔴 CRITICAL  
**Сценарий:** Два потока одновременно вызывают `record_fill()` с одним `fill_id`. Оба проходят проверку `if fill_id in self._seen_fills` (lock отпущен между check и записью в `_seen_fills`). Результат: **двойная запись сделки** → искажение PnL, двойная комиссия.  
**Корневая причина:** Дедупликация (check) и добавление в `_seen_fills` (mark) находятся в разных lock-секциях. Между ними выполняется проекция в order_history + trades_history.  
**Последствия:** Потеря денег через двойной учёт.  
**Исправление:** Переместить `self._seen_fills[fill_id] = time.time()` ВНУТРЬ первого lock-блока, сразу после проверки, ДО проекции. Проекция может быть вне lock (идемпотентна по exec_key в save_order).
**Статус: СОГЛАСЕН.** В текущем коде `record_fill()` действительно сначала проверяет `fill_id` под lock, затем отпускает lock, делает проекцию в `order_history` и `trades_history`, и только потом во второй lock-секции записывает `fill_id` в `_seen_fills`. Окно TOCTOU реально существует.

### РИСК 2: Chase fallback размещает маркет-ордер без проверки partial fills
**Файл:** [core/order_placer.py](core/order_placer.py) ~L187-192  
**Severity:** 🔴 CRITICAL  
**Сценарий:** ChaseOrder заполнен на 50% (5 из 10 контрактов). Таймаут истекает. Fallback вызывает `place_market()` на ПОЛНЫЙ объём (10 контрактов) без проверки `chase.filled_qty`.  
**Последствия:** Позиция 15 контрактов вместо 10 → **лишняя позиция, прямые убытки**.  
**Исправление:** Перед fallback: `remaining = qty - chase.filled_qty; if remaining > 0: place_market(remaining)`.
**Статус: ЧАСТИЧНО СОГЛАСЕН.** В `place_chase()` fallback на market действительно использует исходный `qty`, а не остаток. Но сейчас fallback вызывается только в ветке `if chase.filled_qty == 0`, поэтому конкретно описанный сценарий `5 исполнено, затем market ещё на 10` этим кодом не подтверждается один в один.

### РИСК 3: Stale резервы капитала блокируют торговлю навсегда
**Файл:** [core/reservation_ledger.py](core/reservation_ledger.py) ~L131-145  
**Severity:** 🟠 HIGH  
**Сценарий:** Ордер отправлен, резерв создан, ордер отвергнут биржей, но `release()` не вызван (ошибка в error path). Резерв помечается `stale=True`, но **не удаляется** из `_reservations`.  
**Последствия:** `total_reserved()` включает stale резервы → торговля блокирована → пропущены сигналы → упущенная прибыль.  
**Исправление:** В `_mark_stale()` добавить логику удаления резерва после `stale_cleanup_timeout` (например, 5 минут).
**Статус: СОГЛАСЕН.** `ReservationLedger._mark_stale()` только помечает запись как stale, но не удаляет её и не исключает из `total_reserved()`. В результате stale-резервы продолжают блокировать капитал.

### РИСК 4: Daily loss limit bypass при смене дня без lock
**Файл:** [core/risk_guard.py](core/risk_guard.py) ~L110-141  
**Severity:** 🟠 HIGH  
**Сценарий:** Два потока одновременно вызывают `check_risk_limits()` в момент смены дня (`today != self._today_date`). Оба сбрасывают `_baseline_metric`. Race condition: один поток может использовать old baseline с new metric → лимит обойдён.  
**Корневая причина:** `_today_date` и `_baseline_metric` обновляются без `self._lock`.  
**Последствия:** Стратегия продолжает торговать после достижения дневного лимита убытков.  
**Исправление:** Обернуть весь блок daily_loss_limit в `with self._lock`.
**Статус: СОГЛАСЕН.** В `RiskGuard` поля `_today_date` и `_baseline_metric` в daily-loss ветке обновляются без `self._lock`. Race condition при смене дня действительно возможен.

### РИСК 5: order_in_flight не очищается при update_position() из reconciler
**Файл:** [core/position_tracker.py](core/position_tracker.py) ~L117-121  
**Severity:** 🟠 HIGH  
**Сценарий:** Ордер в полёте (`_order_in_flight=True`). Reconciler обнаруживает mismatch, вызывает `_detect_position()` → `update_position(0, 0, 0)`. Позиция обнулена, но `_order_in_flight` остаётся `True`. Все последующие сигналы отвергаются `try_set_order_in_flight()`.  
**Последствия:** Стратегия «замерзает» — не может открыть новую позицию до перезапуска.  
**Исправление:** В `update_position()` добавить `self._order_in_flight = False` при position == 0.
**Статус: СОГЛАСЕН.** `update_position()` меняет только позицию, количество и цену входа. Флаг `_order_in_flight` не затрагивается, поэтому flat state с зависшим in-flight возможен.

### РИСК 6: STALE_STATE блокирует submission key навсегда
**Файл:** [core/order_executor.py](core/order_executor.py) ~L349, L769  
**Severity:** 🟠 HIGH  
**Сценарий:** `place_order_result()` возвращает `STALE_STATE` (ордер мог быть отправлен или нет — неизвестно). Submission key добавляется в `_blocked_submission_keys`. Последующие сигналы с тем же ключом молча отвергаются — **навсегда**, до перезапуска.  
**Последствия:** Стратегия прекращает торговать по этому тикеру.  
**Исправление:** Добавить TTL для blocked keys (например, 5 минут) или сбрасывать после reconcile.
**Статус: СОГЛАСЕН.** `_blocked_submission_keys` хранится в обычном `set` без TTL. По текущему коду такие ключи не протухают автоматически и могут жить до stop/restart.

### РИСК 7: _last_bar_dt доступ вне _bars_lock
**Файл:** [core/live_engine.py](core/live_engine.py) ~L605-618  
**Severity:** 🟡 MEDIUM  
**Сценарий:** `_load_and_update()` читает `self._last_bar_dt` (L605-618) без `_bars_lock`, а пишет под lock (L620). При concurrent доступе из другого потока (маловероятно, т.к. единственный poll_loop thread) возможен stale read.  
**Последствия:** Теоретически может пропустить один бар (low probability).  
**Исправление:** Читать `self._last_bar_dt` только под `_bars_lock`.
**Статус: СОГЛАСЕН.** Чтение `_last_bar_dt` вне `_bars_lock` есть, запись под `_bars_lock` тоже есть. Практический риск невысокий, но сам пункт по lock-дисциплине верный.

### РИСК 8: Emergency close (manual_close_position) — no retry, one-shot
**Файл:** [core/live_engine.py](core/live_engine.py) ~L447-489  
**Severity:** 🟡 MEDIUM  
**Сценарий:** Оператор нажимает «Закрыть позицию». `place_order_result()` выбрасывает исключение (сеть, DLL call). Метод возвращает `"close_failed"`, позиция остаётся открытой.  
**Последствия:** Оператор должен повторить вручную, но может не заметить.  
**Исправление:** Добавить retry с backoff (2-3 попытки) или явный UI-алерт с кнопкой повтора.
**Статус: СОГЛАСЕН.** `manual_close_position()` делает ровно одну попытку отправки ордера и при ошибке сразу возвращает `close_failed`. Retry-path отсутствует.

### РИСК 9: Reconnect race — множество _reconnect_loop потоков
**Файл:** [core/base_connector.py](core/base_connector.py) ~L344-356  
**Severity:** 🟡 MEDIUM  
**Сценарий:** Disconnect event → запускается `_reconnect_loop()` в новом потоке. Ещё один disconnect → ещё один `_reconnect_loop()`. Два потока конкурентно вызывают `connect()`.  
**Последствия:** Дублированные подключения, непредсказуемое состояние коннектора.  
**Исправление:** Защитить `_reconnect_thread` с помощью `threading.Lock`. Проверять `is_alive()` перед запуском нового.
**Статус: ЧАСТИЧНО СОГЛАСЕН.** Базовая защита уже есть через проверку `self._reconnect_thread.is_alive()`. Но сама проверка не атомарна и не защищена lock-ом, поэтому race при конкурентных вызовах остаётся.

### РИСК 10: Equity flush O(n²) при каждой сделке
**Файл:** [core/trade_recorder.py](core/trade_recorder.py) ~L126-166  
**Severity:** 🟡 MEDIUM  
**Сценарий:** `_flush_equity()` вызывает `get_total_pnl()`, который перечитывает ВСЕ пары из order_history для расчёта кумулятивного PnL. При 1000 сделках каждый вызов = парсинг 1000 записей.  
**Последствия:** Деградация производительности, увеличение латентности сигналов.  
**Исправление:** Кэшировать realized_pnl инкрементально, а не пересчитывать.
**Статус: СОГЛАСЕН.** После каждой сделки вызывается `_flush_equity()`, а внутри него `get_total_pnl()`, который снова идёт по истории сделок. Это действительно создаёт накопительный O(n²)-паттерн.

---

## 🚨 BLOCKER БАГИ

### BLOCKER-1: FillLedger TOCTOU дедупликация
**Описание:** См. РИСК 1 выше. Дубликат fill может пройти мимо проверки.  
**Воспроизведение:** Два concurrent fill с одинаковым `fill_id` (возможно при reconnect + late fill repair).  
**Приоритет:** P0 — исправить немедленно.
**Статус: СОГЛАСЕН.** Это корректный повтор уже подтверждённого РИСК 1: дедупликация и mark разнесены, поэтому blocker реальный.

### BLOCKER-2: Chase fallback без учёта partial fills
**Описание:** См. РИСК 2 выше. Маркет-ордер на полный qty после частичного исполнения chase.  
**Воспроизведение:** Chase на 10 контрактов, исполнено 5, таймаут, fallback market на 10.  
**Приоритет:** P0 — исправить немедленно.
**Статус: ЧАСТИЧНО СОГЛАСЕН.** Повторяет РИСК 2. Ошибка с использованием полного `qty` в fallback есть, но описанный сценарий с partial fill и затем full-size market текущей веткой fallback не подтверждается буквально.

---

## 📋 ДЕТАЛЬНЫЙ АУДИТ ПО СЕКЦИЯМ

---

### 1. 🧱 АРХИТЕКТУРА СИСТЕМЫ

**Разделение слоёв:**
- `core/` (бизнес-логика) → `ui/` (PyQt6 GUI) → `strategies/` (модули стратегий)
- Правило «core/ не импортирует ui/» **соблюдается** ✅
- Обратная связь через Qt-сигналы (`ui_signals`) ✅
- DI-контейнер (`core/di_container.py`) с `register()` / `resolve()` ✅

**Модульность:**
- `ValuationService` — единая точка денежных формул ✅
- `PositionTracker` — чистый state machine с lock ✅
- `OrderExecutor` — чёткий pre-trade gate → placement → monitoring ✅
- `Reconciler` — периодическая сверка engine ↔ broker ↔ history ✅

**Нарушения SOLID:**

| Нарушение | Модуль | Описание |
|-----------|--------|----------|
| SRP | `LiveEngine` (~700 LOC) | Совмещает polling, bar processing, position detection, equity tracking. Не god-class, но на грани |
| OCP | `CommissionManager` | Формулы для futures/stocks жёстко закодированы; добавление нового типа (облигации) требует правки calculate() |
| DIP | `autostart.py` | Прямые импорты `from core.storage import ...` внутри функций; работает, но затрудняет тестирование |

**Coupling:**
- `LiveEngine` → `OrderExecutor` → `PositionTracker` → `Reconciler` — линейная цепочка ✅
- `FillLedger` → `order_history` + `trades_history` — двойная проекция, связность выше необходимой
- `autostart.py` ↔ `live_engine.py` — circular import через deferred import (работает, но хрупко)

**Итого:** Архитектура хорошая для retail trading app. Расширяемость разумная. Нет god-classes.

---

### 2. 🔁 EVENT-DRIVEN СИСТЕМА

**Модель событий:** Polling-based (не push). `_poll_loop()` опрашивает `get_history()` каждые N секунд.

**Порядок событий:**
- Бары приходят отсортированными по времени (из коннектора) ✅
- `on_bar()` вызывается только для закрытых баров (bars[:-1]) ✅
- Защита от обработки старых баров: `if newest_dt <= self._last_bar_dt: return` ✅

**Дубли событий:**
- FillLedger дедупликация по `fill_id` — **TOCTOU** (см. BLOCKER-1) ⚠️
- OrderHistory дедупликация по `exec_key` внутри `write_lock` ✅
- OrderLifecycle: `filled_qty` monotonicity check (L154-159) — out-of-order fills игнорируются ✅
   **Статус: СОГЛАСЕН.** Это ещё один корректный повтор уже подтверждённой проблемы с `fill_id`.

**Потеря событий:**
- При reconnect: `_detect_position()` пересинхронизирует с брокером ✅
- При timeout get_history: `_consecutive_timeouts` счётчик, после 5 → degraded state ✅
- При crash poll_loop: exception caught, loop continues ✅

**Race conditions:**
1. `_process_bar()` читает bars под lock, но `on_precalc()` / `on_bar()` выполняются вне lock — теоретический race с `_load_and_update()`, но на практике оба в одном потоке (`_poll_loop`) ✅
2. `execute_signal()` может быть вызван из poll_loop thread, а `_monitor_market_order()` работает в `_monitor_pool` thread — защищено через `_position_lock` и `try_set_order_in_flight()` ✅

**Идемпотентность:**
- `save_order()` дедуплицирует по `exec_key` ✅
- `record_fill()` дедуплицирует по `fill_id` — с TOCTOU ⚠️
- `try_set_order_in_flight()` атомарный check-and-set ✅
   **Статус: СОГЛАСЕН.** Формулировка соответствует текущему коду: `record_fill()` только частично идемпотентен из-за TOCTOU-окна.

---

### 3. ⚡ LATENCY И ТАЙМИНГ

**Задержки в pipeline:**
```
signal_ts (on_bar) → execute_signal() → place_order() → exchange ack → fill
│                    │                   │                │
│ ~0ms (sync call)   │ pre-trade gate    │ DLL call       │ exchange latency
│                    │ ~1-5ms            │ ~50-200ms      │ ~0-1000ms
```

**Проверка актуальности данных:**
- `signal_latency_budget_sec` = 10.0 (настраиваемый) — сигнал отвергается, если `time.time() - signal_ts > budget` ✅
- `stale_quote_budget_ms` = 5000 — котировка отвергается, если старше 5с ✅
- `MarketDataEnvelope` содержит `data_ts`, `receive_ts`, `age_ms` ✅

**Проблемы:**
- **Нет проверки серверного времени** — полагается на локальные часы. Если часы сбиты, все проверки stale бесполезны.
- `get_history()` timeout = 10с (Finam) / 30с (QUIK) — в это время стратегия заморожена.
- Между генерацией сигнала и реальным исполнением проходит: poll_interval + history_fetch + precalc + execute_signal + place_order ≈ 2-35 секунд.
**Статусы:**
- **ЧАСТИЧНО СОГЛАСЕН** по серверному времени: явной синхронизации с серверными часами нет, но это скорее hardening-gap, чем уже доказанный runtime-баг.
- **СОГЛАСЕН** по `get_history()` timeout: poll path реально блокируется на ожидании timeout.
- **ЧАСТИЧНО СОГЛАСЕН** по диапазону `2-35 секунд`: архитектурно задержка действительно может складываться из перечисленных этапов, но сам диапазон в review оценочный.

---

### 4. 📡 MARKET DATA INTEGRITY

**Валидация баров:**
- `_validate_bars()` проверяет OHLC consistency (high ≥ low, close > 0) ✅
- При invalid bars → `_require_manual_intervention()` — стратегия блокируется ✅
- MarketDataEnvelope проверяет staleness ✅

**Пропуски тиков:**
- Polling-based модель: пропуск тика = пропуск poll_interval. Не критично для bar-based стратегий.
- Нет fallback источников market data (только один коннектор).

**«Залипшие» цены:**
- `stale_quote_budget_ms` = 5000 (по умолчанию) — довольно агрессивно для MOEX pre-clearing.
- **Проблема:** Если биржа не обновляет цены (auction, halt), 5с бюджет приведёт к rejection сигналов во время аукциона.
   **Статус: ЧАСТИЧНО СОГЛАСЕН.** Код действительно отбрасывает старые котировки по фиксированному freshness-budget, так что в режимах редких обновлений это может блокировать сигналы. Но это скорее жёсткая настройка и операционный компромисс, чем универсальный баг.

**Дубли:** Polling-модель не генерирует дубли — каждый poll возвращает snapshot.

---

### 5. 📊 ОРДЕР-МЕНЕДЖМЕНТ

**Создание ордеров:**
```
execute_signal() → pre-trade gate → submission check → placement → monitoring
```

**Pre-trade gate ([order_executor.py](core/order_executor.py) L333-450):**
1. Circuit breaker check ✅
2. Risk limits (max_position_size, daily_loss_limit) ✅
3. Account-level risk (gross exposure, positions count) ✅
4. Submission blocking (STALE_STATE protection) ✅
5. `try_set_order_in_flight()` — атомарный TOCTOU elimination ✅
6. Capital reservation ✅
7. Signal latency check ✅
8. Stale quote check ✅

**Отмена ордеров:**
- Chase: `cancel()` → `_cancel_and_wait()` (2с ожидание terminal status) ✅
- Limit price: cancel при достижении TRADING_END_TIME_MIN (23:45) ✅
- **Проблема:** `cancel_order()` в limit price мониторе **не имеет timeout** — может зависнуть при network hang.
   **Статус: СОГЛАСЕН.** В monitor-потоке limit-price вызывается прямой `self.connector.cancel_order(...)` без timeout-wrapper. Если зависнет сам коннектор, этот поток действительно может повиснуть.

**Retry:**
- Market order: **нет retry** — one-shot placement ✅ (правильно для market orders)
- Chase: retry внутри `_run()` loop с restatement каждые 0.2-0.3с ✅
- **Проблема:** Chase retry при persistent place_order error — бесконечный loop с 1с delay, без backoff.
   **Статус: ЧАСТИЧНО СОГЛАСЕН.** На persistent `place_order` failure chase действительно ретраит с фиксированной паузой 1 секунда и без backoff. Но loop ограничен внешним timeout у `place_chase()`, поэтому слово «бесконечный» здесь завышает риск.

**Статусы:**
- `OrderLifecycle` state machine: WORKING → PARTIAL_FILL → MATCHED/CANCELED/DENIED ✅
- Terminal states: `{MATCHED, CANCELED, DENIED, REMOVED, EXPIRED, KILLED}` ✅
- Late fill detection: TERMINAL → LATE_FILL_REPAIR если filled увеличивается ✅

**Дубли ордеров:**
- Submission key blocking (`_make_submission_key()`) предотвращает повторную отправку ✅
- `try_set_order_in_flight()` предотвращает parallel entry ✅
- **⚠️ STALE_STATE**: submission key заблокирован навсегда (см. РИСК 6)
   **Статус: СОГЛАСЕН.** Это повтор уже подтверждённого РИСК 6: TTL или автоочистки blocked submission keys сейчас нет.

---

### 6. 📈 ИСПОЛНЕНИЯ (FILLS)

**Partial fills:**
- `_monitor_market_order()` обрабатывает partial fills через `OrderLifecycle.update_from_connector()` ✅
- `PositionTracker.close_position(filled, total_qty)` корректно обрабатывает частичное закрытие ✅
- Chase: watcher callback обновляет `_filled_qty` при каждом fill ✅

**Агрегация:**
- FIFO-пары в `get_order_pairs()` — корректная агрегация по side ✅
- Commission slice при partial close: `_slice_commission()` пропорционально ✅

**Double count:**
- FillLedger `_seen_fills` дедупликация — **TOCTOU** (см. BLOCKER-1) ⚠️
- OrderHistory `exec_key` дедупликация внутри write_lock ✅
   **Статус: СОГЛАСЕН.** Разделение корректно описано: дедупликация по `fill_id` слабее и гоняется, а durable дедуп по `exec_key` внутри истории жёстче.

**Late fills:**
- `OrderLifecycle.get_late_fill_delta()` определяет дельту ✅
- `PendingOrderRegistry.check_late_fills()` периодически проверяет ✅
- При late fill: Telegram алерт + audit event ✅

---

### 7. 📦 ПОЗИЦИИ

**State machine ([position_tracker.py](core/position_tracker.py)):**
```
     TRADE PATH                       SYNC PATH
  ┌────────────────┐              ┌─────────────────┐
  │ flat → long    │ open_position│ any → any       │ update_position
  │ flat → short   │              │ (no validation) │
  │ long → flat    │ close_position│                 │
  │ short → flat   │              └─────────────────┘
  │                │
  │ FORBIDDEN:     │
  │ long → short   │ confirm_open → returns False
  │ short → long   │
  │ long → long+   │ (scale-in)
  └────────────────┘
```

**Потокобезопасность:**
- Все методы защищены `_position_lock` ✅
- `try_set_order_in_flight()` — атомарный check-and-set ✅
- `try_set_order_in_flight_for_close()` — отдельный метод для close path ✅

**Проблемы:**
- `update_position()` — **sync path без валидации**. Может установить любое состояние. Это by design для reconciliation, но опасно: см. РИСК 5 (order_in_flight не очищается).
- `close_position()` при partial fill — `entry_price` не пересчитывается. Это корректно (позиция уменьшается, но средняя цена та же).
   **Статус: СОГЛАСЕН.** Сам по себе sync path без валидации здесь допустим архитектурно, но в связке с неочисткой `order_in_flight` он действительно оставляет опасную дыру, описанную в РИСК 5.

---

### 8. 🧮 PnL (КРИТИЧНО)

**Формулы:**

**Realized PnL (закрытая пара, FIFO):**
```
gross_pnl_long  = (close_price − open_price) × qty × pnl_multiplier
gross_pnl_short = (open_price − close_price) × qty × pnl_multiplier
net_pnl = gross_pnl − entry_commission − exit_commission
```
Реализовано в `ValuationService.compute_closed_pnl()` с `Decimal` арифметикой ✅

**Unrealized PnL (открытая позиция):**
```
unrealized = (current_price − entry_price) × qty × pnl_multiplier − entry_commission − exit_commission
```
Реализовано в `ValuationService.compute_open_pnl()` с `Decimal` ✅

**Числовой пример (фьючерс Si):**
```
Вход: buy 1 @ 95000, point_cost = 1.0
Выход: sell 1 @ 95500
MOEX commission (taker 0.001%): 95000 × 1.0 × 1 × 0.001 / 100 = 0.95₽
Broker commission: 1.00₽ × 1 = 1.00₽
Entry commission = 0.95 + 1.00 = 1.95₽
Exit commission = 95500 × 0.001 / 100 + 1.00 = 1.955₽

gross_pnl = (95500 − 95000) × 1 × 1.0 = 500₽
net_pnl = 500 − 1.95 − 1.955 = 496.095₽
```

**pnl_multiplier resolution:**
```
futures  → point_cost (стоимость пункта цены, загружается из broker)
stocks   → lot_size (количество бумаг в лоте)
bonds    → lot_size (обычно 1)
```
Реализовано в `ValuationService.get_pnl_multiplier()` ✅

**Equity snapshot:**
```
equity = realized_pnl + unrealized_pnl
realized_pnl = Σ net_pnl по всем закрытым парам
unrealized_pnl = compute_open_pnl(entry, current, qty, multiplier, commissions)
```

**Проблемы PnL:**
1. `_flush_equity()` пересчитывает realized_pnl через O(n) scan всех пар на каждую сделку ⚠️
2. `exit_commission` в unrealized PnL считается по `current_price` — это оценка, не факт (корректно)
3. `max_drawdown` нормализуется на текущий `position_qty` — нестабильна при пирамидинге ⚠️
**Статусы:**
1. **СОГЛАСЕН.** `_flush_equity()` после каждой сделки тянет полный пересчёт realized PnL.
2. **ГАЛЮЦИНАЦИЯ.** Это не дефект, а стандартная оценочная компонента для unrealized PnL. Сам audit уже оговаривает, что это корректно.
3. **СОГЛАСЕН.** `equity_tracker.record_equity()` делит просадку на текущий `position_qty`, поэтому метрика меняется вместе с размером позиции.

---

### 9. 💰 КОМИССИИ

**Формулы:**

**Фьючерсы:**
```
trade_value = price × point_cost × quantity
moex_part = trade_value × moex_taker_pct / 100      # 0.001% для валютных
broker_part = broker_futures_rub × quantity           # 1.00₽/контракт для валютных
total = moex_part + broker_part                       # за одну сторону
```

**Акции:**
```
trade_value = price × quantity × lot_size
moex_part = trade_value × moex_taker_pct / 100       # 0.003% для TQBR
broker_part = trade_value × broker_stock_pct / 100    # 0.04%
total = moex_part + broker_part                       # за одну сторону
```

**Maker/Taker:**
- Maker pct для фьючерсов = 0 (MOEX programme Maker-0) ✅
- Taker pct = из конфига `data/commission_config.json` ✅

**Partial fills:** Комиссия пропорционально разделяется в `_slice_commission()`:
```
slice = total_commission × (close_qty / open_qty)
```
Возможна потеря точности при дроблении (float arithmetic), но на практике ~0.01₽ ⚠️
**Статус: ГАЛЮЦИНАЦИЯ.** В текущем коде `_slice_commission()` идёт через `valuation_service.slice_commission()`, а там деление выполняется через `Decimal`, не через голый float.

**Проблемы:**
1. `CommissionManager.calculate()` использует float, не Decimal — потеря точности ~0.00001₽ ⚠️
2. Конфиг по коннекторам (`broker_transaq` vs `broker_quik`) — корректно разделён ✅
3. MOEX тарифы зашиты в JSON, обновляются вручную. Нет автоматической синхронизации с биржей.
**Статусы:**
1. **СОГЛАСЕН.** `CommissionManager.calculate()` действительно считает на float.
2. Этот пункт не описывает дефект, поэтому статус не требуется.
3. **ЧАСТИЧНО СОГЛАСЕН.** Фоновой автосинхронизации ставок из MOEX я не нашёл. Но инфраструктура обновления в коде уже есть, поэтому тезис про полностью статичную модель устарел.

---

### 10. 💵 БАЛАНС

**Available / Reserved:**
- `ReservationLedger` управляет резервированием капитала ✅
- `reserve(key, account_id, amount)` → атомарное резервирование с lock ✅
- `release(key)` → снятие резерва ✅
- `total_reserved(account_id)` → сумма всех резервов ✅

**Проблема магических денег:**
- Stale резервы помечаются, но **не удаляются** → `total_reserved()` завышен → деньги «заморожены» (см. РИСК 3)
- `bind_order()` привязывает резерв к конкретному ордеру — позволяет отслеживать lifecycle ✅
   **Статус: СОГЛАСЕН.** Это прямой повтор РИСК 3: stale-записи остаются в ledger и продолжают участвовать в reserved capital.

---

### 11. 🔢 ЧИСЛЕННАЯ ТОЧНОСТЬ

**Decimal vs Float:**
- `ValuationService`: все PnL формулы через `to_decimal()` → `Decimal` ✅
- `money.py`: `MONEY_QUANT = Decimal("0.00000001")` (8 знаков) ✅
- `to_storage_float()` / `to_storage_str()` — нормализация перед записью ✅
- `CommissionManager.calculate()` — **float** арифметика ⚠️
- `risk_guard.check_risk_limits()` — **float** сравнение ⚠️

**Rounding:**
- `ROUND_HALF_UP` для финальной нормализации ✅
- `order_history.json` хранит `price_decimal` (string) alongside `price` (float) ✅

**Tick size:**
- Нет явной проверки tick size при размещении ордера. Это делегировано бирже (MOEX отвергнет некорректную цену). Допустимо для market orders.

**Накопление ошибок:**
- FIFO-пары: каждая пара считается отдельно через Decimal → накопления нет ✅
- `_slice_commission()` при partial fills: float деление → минимальное накопление ⚠️
   **Статус: ГАЛЮЦИНАЦИЯ.** Деление комиссии по partial fills сейчас выполняется через `Decimal` в `valuation_service`, так что описанная float-проблема в текущем коде не подтверждается.

---

### 12. 🔄 RECONCILIATION

**Механизм ([reconciler.py](core/reconciler.py)):**
```
reconcile_result():
  1. Skip if order_in_flight ✅
  2. check_late_fills() ✅
  3. Get engine_qty (PositionTracker)
  4. Get broker_qty (connector.get_positions())
  5. Get history_qty (order_history unclosed pairs)
  6. Compare:
     engine ≠ broker → log + alert + self_heal
     history ≠ broker → log + alert + callback/self_heal
```

**Интервал:** 60 секунд (настраиваемый через `reconcile_interval_sec`) ✅

**Self-heal:** Вызывает `_detect_position()` — READ from broker, UPDATE engine state. Неразрушающий ✅

**Проблемы:**
1. `_get_history_qty()` возвращает 0 при exception → может создать ложный mismatch с broker ⚠️
2. Broker data unavailable → пропуск reconcile (правильно), но `on_broker_unavailable()` callback может отсутствовать ⚠️
3. Self-heal вызывается при **каждом** mismatch (нет «подождать N циклов»). Cooldown только на alert (300с), не на self-heal.
**Статусы:**
1. **СОГЛАСЕН.** На exception `_get_history_qty()` реально возвращает 0.
2. **ЧАСТИЧНО СОГЛАСЕН.** Callback опционален, но отсутствие callback само по себе не ломает reconcile; это скорее ограничение extensibility.
3. **СОГЛАСЕН.** Cooldown сейчас стоит только на alert, а не на self-heal.

**Источник истины:**
- **Broker** — primary source of truth для positions ✅
- **Order history** — source of truth для PnL ✅
- **Engine** — рабочая копия, синхронизируется с broker через reconcile ✅

---

### 13. 🔌 ОТКАЗОУСТОЙЧИВОСТЬ

**Reconnect:**
- `BaseConnector._reconnect_loop()` — reconnect с backoff ✅
- **Проблема:** Нет защиты от множественных reconnect потоков (см. РИСК 9)
- При reconnect → `_detect_position()` resync ✅
   **Статус: ЧАСТИЧНО СОГЛАСЕН.** См. РИСК 9: защита есть, но не атомарная.

**Retry:**
- Market order: no retry (one-shot) — корректно для market
- Chase: retry внутри loop — корректно
- Emergency close: no retry — **ПРОБЛЕМА** (см. РИСК 8)
   **Статус: СОГЛАСЕН.** Это повтор уже подтверждённого РИСК 8.

**Таймауты:**
| Компонент | Timeout | Оценка |
|-----------|---------|--------|
| Market order monitor | 45с | ✅ Адекватно для MOEX |
| Chase order total | 120с | ⚠️ Долго, капитал заморожен |
| get_history (Finam) | 10с | ✅ |
| get_history (QUIK) | 30с | ✅ |
| Preflight connector wait | 30с | ✅ |
| Limit price monitor | до 23:45 | ✅ |

**Двойные ордера при reconnect:**
- Submission key blocking предотвращает дубли ✅
- `try_set_order_in_flight()` предотвращает parallel entry ✅
- PendingOrderRegistry восстанавливает ордера после рестарта ✅

---

### 14. 🧠 STATE MACHINE

**StrategyRuntimeState:**
```
STOPPED → INITIALIZING → SYNCED → TRADING
                       ↘ DEGRADED
                       ↘ STALE
                       ↘ MANUAL_INTERVENTION_REQUIRED
         STOPPING → STOPPED
         FAILED_START
```

**Переходы:**
- `STOPPED → INITIALIZING`: `start_live_engine()` ✅
- `INITIALIZING → SYNCED`: `_detect_position()` success ✅
- `SYNCED → TRADING`: `engine.start()` success ✅
- `TRADING → DEGRADED`: 5+ consecutive timeouts ✅
- `TRADING → STOPPING → STOPPED`: `stop_live_engine()` ✅
- `DEGRADED → TRADING`: successful reconnect + resync ✅

**Невозможные состояния:**
- `TRADING` с `sync_status=stale` — допустимо, opening signals blocked ✅
- `STOPPED` с engine in `_live_engines` — невозможно, engine удаляется при stop ✅

---

## 🚨 15. LIFECYCLE (КЛЮЧЕВОЙ БЛОК)

### Старт

**Невозможность старта до готовности:**
1. `connector.is_connected()` проверяется ✅
2. `is_in_schedule()` проверяется ✅
3. `_claim_strategy_ownership()` проверяет коллизии ✅
4. `startup_preflight()` синхронизирует позицию с broker ✅
5. `call_on_start()` вызывает пользовательский hook ✅

**Атомарность:** `_engine_state_lock` + `_launching_engines` dict предотвращает двойной запуск ✅

### Инициализация

**Порядок ([autostart.py](core/autostart.py) L215-340):**
```
1. connect → _wait_for_connector(timeout=30) ✅
2. _claim_strategy_ownership() ✅
3. LiveEngine() constructor → creates components ✅
4. startup_preflight() → _detect_position() → reconcile with broker ✅
5. call_on_start(params, connector) → user hook ✅
6. engine.start() → _poll_loop thread ✅
7. Register in _live_engines ✅
```

**Проблема:** Между `startup_preflight()` (step 4) и `engine.start()` (step 6) позиция может измениться на бирже. Это gap ~100ms. Reconcile в poll_loop исправит через 60с. Допустимо.
**Статус: ЧАСТИЧНО СОГЛАСЕН.** Окно между preflight и стартом poll loop действительно есть, но это короткий архитектурный gap, а не явный дефект текущей реализации. Скорее известный компромисс.

### Рестарт

**Восстановление позиций:**
- `startup_preflight()` → `_detect_position()` → READ from broker ✅
- Позиция восстанавливается из broker, не из файла ✅

**Восстановление ордеров:**
- `PendingOrderRegistry._load_from_storage()` восстанавливает из `pending_orders.json` ✅
- `recover_strategy_orders()` проверяет статус у broker ✅

**Обработка partial fills:**
- При рестарте: `check_late_fills()` обнаруживает fills, произошедшие во время downtime ✅
- Late fill → LATE_FILL_REPAIR state + Telegram alert ✅

### Дубли ордеров при рестарте

**Сценарий:** Ордер отправлен → ответ не получен (crash) → повторная отправка.
- `PendingOrderRegistry` сохраняет tid → при рестарте проверяет статус ✅
- `submission_key` blocking не persisted → после рестарта может быть снято ⚠️
- **Защита:** `try_set_order_in_flight()` восстанавливается из `_detect_position()`, который ставит position state ✅
   **Статус: ЧАСТИЧНО СОГЛАСЕН.** Неперсистентность `submission_key` действительно есть. Но audit сам же отмечает рабочие компенсирующие механизмы через `PendingOrderRegistry` и resync позиции, поэтому это не чистый blocker дубля, а ослабление идемпотентности после рестарта.

### Повторный запуск стратегии

- `_launching_engines[strategy_id] = True` — атомарная защита от double-start ✅
- `if strategy_id in _live_engines: return True` — already running check ✅
- `finally: _launching_engines.pop(strategy_id, None)` — cleanup ✅

### Подписки

- Quote subscription восстанавливается в `_poll_loop()` через `_subscribe_quotes()` ✅
- Reconnect callback: `_on_connector_reconnect()` → `_detect_position()` ✅
- Watchdog: `_sync_engines_with_connectors()` — пересинхронизация при reconnect коннектора ✅

---

### 16. 🧪 EDGE CASES

| Edge Case | Обработка | Оценка |
|-----------|----------|--------|
| Partial fill + cancel | `close_position(filled, total_qty)` корректно уменьшает qty | ✅ |
| Duplicate events (fill_id) | FillLedger `_seen_fills` check (TOCTOU!) | ⚠️ |
| Reconnect during order | PendingOrderRegistry + late fill detection | ✅ |
| Высокая волатильность | `stale_quote_budget_ms` может reject сигналы | ⚠️ |
| get_history timeout | 5 consecutive → degraded state + Telegram alert | ✅ |
| Биржевой halt | Стратегия продолжает poll, сигналы не генерируются | ✅ |
| Midnight rollover | `_today_date` reset в risk_guard (race condition!) | ⚠️ |
| Два сигнала подряд | `try_set_order_in_flight()` блокирует второй | ✅ |
| Connector disconnect во время order | Order monitor продолжает poll, connector errors logged | ✅ |

---

### 17. 📉 РЕАЛИЗМ

**Slippage:**
- Market orders: slippage не моделируется (исполнение по рыночной цене) — корректно для live trading.
- Chase orders: limit at best bid/offer — минимизирует slippage ✅
- **Backtest:** Backtest engine может учитывать slippage через параметры (отдельный модуль).

**Liquidity:**
- Нет проверки объёма стакана перед размещением ордера. Для retail volumes на MOEX — допустимо.
- Для крупных ордеров (>100 контрактов Si) нет TWAP/VWAP — ограничение.

---

### 18. 💱 МУЛЬТИВАЛЮТНОСТЬ

- Все расчёты в рублях (RUB) ✅
- Для валютных фьючерсов (CR, Si) — PnL через `point_cost` в рублях ✅
- Нет поддержки мультивалютных портфелей — ограничение, но для MOEX не критично.

---

### 19. 🛡 РИСК-МЕНЕДЖМЕНТ

**RiskGuard ([risk_guard.py](core/risk_guard.py)):**

| Проверка | Реализация | Оценка |
|----------|-----------|--------|
| Circuit breaker | 3 consecutive failures → block | ✅ |
| max_position_size | qty > limit → reject | ✅ |
| daily_loss_limit | baseline metric policy | ⚠️ Race |
| Account gross exposure | `_check_account_risk_limits()` | ✅ |
| Account positions count | max concurrent positions | ✅ |

**Circuit breaker:**
- Threshold: 3 (настраиваемый) ✅
- Timeout: 60с между failures для reset counter ✅
- Reset: `reset_circuit_breaker()` — ручной сброс ✅
- **Действие при срабатывании:** Блокирует новые ордера. **НЕ закрывает позиции** ✅

**Проблемы:**
1. Daily loss limit: race condition при смене дня (см. РИСК 4)
2. Circuit breaker: `except Exception: pass` при расчёте daily loss — ошибка расчёта → лимит обойдён ⚠️
3. Нет per-instrument risk limits (только per-strategy)
**Статусы:**
1. **СОГЛАСЕН.** Это повтор РИСК 4.
2. **СОГЛАСЕН.** Ошибка в расчёте daily loss сейчас действительно глушится, после чего метод разрешает торговлю дальше.
3. **СОГЛАСЕН.** Per-instrument risk limits в текущем `RiskGuard` нет, только strategy/account-level проверки.

---

### 20. 🧪 ИНВАРИАНТЫ

**Проверяемые:**
1. `позиция в engine = позиция у broker` — reconciler каждые 60с ✅
2. `позиция в history = позиция у broker` — reconciler each cycle ✅
3. `order_in_flight ⊻ можно отправить ордер` — атомарный check ✅
4. `qty(position) = Σ qty(fills)` — implicitly through FillLedger ✅
5. `equity = realized + unrealized` — ValuationService formula ✅

**Не проверяемые:**
1. `reserved_capital ≤ available_capital` — нет explicit invariant check
2. `commission(pair) = commission(open) + commission(close)` — нет cross-check
3. `Σ positions(all strategies on account) = broker total` — нет aggregate reconcile
**Статус: СОГЛАСЕН.** Явных автоматических проверок этих инвариантов в текущем коде не видно. Особенно третий пункт совпадает с отсутствием aggregate reconcile в core.

---

### 21. 🧾 ЛОГИ И АУДИТ

**Полнота логов:**
- Loguru с ротацией (14 дней, zip) ✅
- `enqueue=True` — потокобезопасная запись ✅
- Формат: `{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {name}:{line} — {message}` ✅

**Audit trail:**
- `runtime_metrics.emit_audit_event()` для ключевых событий ✅
- Flatten: `flatten_requested`, `flatten_submitted`, `flatten_confirmed`, `flatten_manual_intervention` ✅
- Reconcile: `history_divergence` audit event ✅
- Все ордера записываются в `order_history.json` с timestamp ✅
- Все сделки в `trades_history.json` ✅

**Восстановление событий:**
- Из order_history.json — можно reconstructить все пары (FIFO) ✅
- Из логов — полная цепочка signal → order → fill ✅
- **Проблема:** trades_history.json ограничен `_MAX_TRADES_HISTORY = 10000` — старые сделки удаляются ⚠️
   **Статус: СОГЛАСЕН.** Ограничение `_MAX_TRADES_HISTORY = 10000` есть, старые записи реально отрезаются при append.

---

### 22. 🧠 ЧЕЛОВЕЧЕСКИЕ ОШИБКИ

| Ошибка | Защита | Оценка |
|--------|--------|--------|
| Двойной запуск стратегии | `_launching_engines` + `_live_engines` check | ✅ |
| Неправильные параметры | Валидация в `on_start()` (зависит от стратегии) | ⚠️ Зависит |
| Остановка с открытой позицией | Warning в логе + позиция сохраняется | ✅ |
| Одинаковый тикер на двух стратегиях | `_claim_strategy_ownership()` prevention | ✅ |
| Запуск вне расписания | `is_in_schedule()` check | ✅ |
| Случайное закрытие вместо остановки | `DestructiveActionGuard` + confirmation dialog | ✅ |
| Остановка агента без confirmation | **НЕТ** confirmation dialog для stop | ⚠️ |
**Статусы:**
- **ЧАСТИЧНО СОГЛАСЕН** по пункту с неправильными параметрами: review верно пишет, что защита зависит от конкретной стратегии, централизованной валидации тут нет.
- **СОГЛАСЕН** по пункту с остановкой агента без confirmation: в `strategy_window.py` `_stop_strategy()` вызывается напрямую по кнопке stop без отдельного подтверждающего диалога.

---

## 🖥 23. UI/UX (КРИТИЧЕСКИЙ РИСК)

### Позиции

- Отображение: через `PositionsPanel` с таблицей + live refresh ✅
- Направление: `buy`/`sell` side из broker positions ✅
- Объём: синхронизация с broker через periodic refresh ✅
- **Проблема:** `_on_close_position()` читает qty при рендере таблицы, а не при нажатии кнопки → stale qty при быстром изменении ⚠️
   **Статус: ЧАСТИЧНО СОГЛАСЕН.** Для полного close этот тезис не подтверждается: `_on_close_position()` передаёт только ticker, а фактический объём потом читается глубже из актуальной позиции. Но похожий stale-risk действительно есть у partial close, где `max_qty` захватывается при рендере кнопки.

### PnL

- Соответствие реальности: ValuationService формулы ✅
- Учёт комиссий: entry + exit commission ✅
- **Проблема:** Unreaalized PnL использует `current_price` для exit_commission estimation — допустимая неточность
   **Статус: ГАЛЮЦИНАЦИЯ.** Это не баг, а ожидаемая оценочная модель unrealized PnL. Сам пункт формулирует «допустимая неточность», а не дефект.

### Ордера

- Статусы через `OrderLifecycle` state machine ✅
- Partial fills: `filled_qty` / `total_qty` отображается ✅

### Состояние системы

- Подключение коннектора: видно ✅
- Sync status: видно ✅
- Runtime state: TRADING / DEGRADED / STALE видно ✅

### Действия

- Защита от двойных кликов: `DestructiveActionGuard` ✅
- **Нет debounce** на кнопках — можно spam-кликать ⚠️
- **Нет confirmation** для stop agent ⚠️
**Статусы:**
- **СОГЛАСЕН** по отсутствию debounce: time-based debounce я не нашёл, есть только `DestructiveActionGuard` на время выполнения destructive action.
- **СОГЛАСЕН** по отсутствию confirmation для stop agent: отдельного stop-confirm dialog в текущем UI нет.

### Где пользователь может потерять деньги через UI:

1. **Stale qty при close** — закроет не ту позицию
2. **Stop agent без confirmation** — случайно сиротит позицию
3. **Button spam** — теоретически может двойной close (DestructiveActionGuard должен помочь)
**Статусы:**
1. **ЧАСТИЧНО СОГЛАСЕН.** Для полного close claim завышен, для partial close похожий риск есть.
2. **СОГЛАСЕН.** Остановка стратегии без подтверждения может оставить открытую позицию под ответственность оператора.
3. **ЧАСТИЧНО СОГЛАСЕН.** Жёсткого debounce нет, но `DestructiveActionGuard` уже уменьшает риск дубля, так что это не голая дыра.

---

### 24. 🧪 ТЕСТИРУЕМОСТЬ

**Покрытие:**
| Модуль | Тестовый файл | Методы | Оценка |
|--------|--------------|--------|--------|
| LiveEngine | test_live_engine*.py | 50+ | ✅ |
| OrderExecutor | test_order_executor*.py | 43 | ✅ |
| PositionTracker | test_position_tracker*.py | 28 | ✅ |
| ValuationService | test_valuation*.py | 30 | ✅ |
| FillLedger | test_fill_ledger*.py | 20+ | ✅ |
| Reconciler | test_reconciler*.py | 15+ | ✅ |
| Financial regression | test_financial_regression*.py | 74 | ✅ |
| StrategyFlatten | test_flatten*.py | 15+ | ✅ |
| CommissionManager | test_commission*.py | 10+ | ✅ |
| FinamConnector locks | test_finam_lock_discipline*.py | 20 | ✅ |
| QuikConnector | test_quik_connector*.py | 1 | ⚠️ GAP |
| UI components | test_ui*.py | 5 | ⚠️ GAP |
| Strategy Loader | N/A | 0 | ⚠️ GAP |

**Статусы:**
- **СОГЛАСЕН** по gap в QuikConnector: в текущем дереве действительно видно только один `test_quik_connector_contract.py`.
- **ЧАСТИЧНО СОГЛАСЕН** по UI gap: UI-тесты действительно почти отсутствуют, но формулировка `5` не совпадает с текущим деревом, где найден как минимум `test_ui_safety.py`.
- **СОГЛАСЕН** по Strategy Loader gap: отдельных тестов для loader я не нашёл.

**Integration tests:** Нет ✅ (допустимо — unit tests с мощным mocking покрывают paths)
**Simulation:** Backtest engine существует (`core/backtest_engine.py`) ✅

---

### 25. 🧠 КОНФИГУРАЦИЯ

**Валидация параметров:**
- `on_start()` в стратегиях — зависит от разработчика стратегии ⚠️
- `max_position_size`, `daily_loss_limit` — проверяются в RiskGuard ✅
- `order_mode` — validated: `market` | `chase` | `limit_price` ✅
   **Статус: ЧАСТИЧНО СОГЛАСЕН.** Централизованной обязательной схемы валидации параметров стратегии в core действительно нет; качество валидации зависит от самой стратегии.

**Дефолты:**
- `poll_interval` = из timeframe (5 мин → 300с) ✅
- `circuit_breaker_threshold` = 3 ✅
- `signal_latency_budget_sec` = 10.0 ✅
- `stale_quote_budget_ms` = 5000 ✅

**Runtime изменения:**
- Параметры стратегий — из `data/strategies.json`, перечитываются при старте ✅
- **Нет hot-reload** параметров без рестарта стратегии — безопасно ✅

---

### 26. 🔐 БЕЗОПАСНОСТЬ

**API ключи:**
- Хранение: `app_profile/secrets.local.json` с **DPAPI шифрованием** ✅
- `SENSITIVE_SETTING_KEYS` — список чувствительных настроек ✅
- **Проблема:** Если DPAPI недоступен → секреты записываются **plaintext** ⚠️ (см. storage.py fallback)
   **Статус: ГАЛЮЦИНАЦИЯ.** В текущем `storage.py` при проблемах с DPAPI код не откатывается в plaintext-запись, а выбрасывает исключение. Утверждение про plaintext fallback устарело.

**Доступы:**
- Локальное десктоп-приложение — нет сетевого API (кроме health_server) ✅
- Health server: HTTP на localhost ✅
- Telegram bot: credentials в secrets ✅

---

### 27. 🏦 ОГРАНИЧЕНИЯ БИРЖИ

**Rate limits:**
- DLL send_command serialized through `_lock` — естественный rate limit ✅
- Нет explicit rate limiter (MOEX не жёстко ограничивает retail) — допустимо

**Min size:**
- Проверяется через `get_sec_info()` → `min_lot` при необходимости ✅
- `_calc_dynamic_qty()` в OrderExecutor учитывает минимальный лот ✅

**Precision:**
- Цена: ордер отправляется as-is, биржа валидирует tick size ✅
- Qty: integer контракты/лоты ✅

---

### 28. 🔄 ВЕРСИОНИРОВАНИЕ

**Совместимость:**
- `order_history.json` — flat JSON, backward compatible ✅
- `strategies.json` — backward compatible (новые поля = optional) ✅
- `commission_config.json` — backward compatible ✅
- **Нет schema version marker** — при структурных изменениях нет автоматической миграции ⚠️
   **Статус: ЧАСТИЧНО СОГЛАСЕН.** Для основных runtime JSON-файлов version marker и явной миграционной схемы действительно нет. Но в проекте уже есть как минимум `_SECRET_STORE_VERSION` для хранилища секретов, так что claim в абсолютной форме слишком широкий.

---

### 29. 📊 ИСТОРИЧЕСКИЕ ДАННЫЕ

**Инициализация индикаторов:**
- `on_precalc(df, params)` вызывается на полном наборе закрытых баров ✅
- `lookback` параметр определяет минимальное количество баров ✅
- `if len(processed_bars) < min(10, lookback): return` — skip insufficient data ✅

**Проблемы:**
- При первом старте (cold start) может не хватить баров для корректного расчёта длинных индикаторов (SMA-200 и т.п.)
- get_history запрашивает `days = max(lookback // 50, 5)` дней — может быть недостаточно для lookback=1000
**Статусы:**
- **ЧАСТИЧНО СОГЛАСЕН.** Недостаток истории на cold start зависит от таймфрейма и источника, но сама возможность такого состояния реальна.
- **СОГЛАСЕН.** Формула `days = max(lookback // 50, 5)` действительно эвристическая и может недобрать историю для больших lookback.

---

### 30. ⚙️ ПРОИЗВОДИТЕЛЬНОСТЬ

**Утечки памяти:**
- `_seen_fills` в FillLedger: ограничен `_MAX_SEEN`, cleanup каждые 24ч ✅
- `_bars` в LiveEngine: ограничен lookback ✅
- `_cache` в storage.py: TTL 2с ✅
- `_order_status` в FinamConnector: TTL-based cleanup (3600с) ✅

**Деградация:**
- `get_total_pnl()` = O(n) пар. При 10000+ пар ≈ 10мс. На каждый flush equity. Допустимо.
- `_flush_equity()` на каждую сделку — O(n²) accumulation. При 100 сделок/день ≈ секунды. ⚠️
   **Статус: СОГЛАСЕН.** Это повтор уже подтверждённого performance-пункта про equity flush.

**Thread pool:**
- `_monitor_pool`: `max_workers=4` — достаточно для 1-2 стратегий, может быть мало для 10+ ⚠️
- `_history_pool`: `max_workers=1` — корректно (serialize history fetch)
   **Статус: ЧАСТИЧНО СОГЛАСЕН.** Значение `4` действительно жёстко зафиксировано, но вывод «мало для 10+ стратегий» остаётся предположением без нагрузочного профиля.

---

### 31. 🧠 ПОВЕДЕНИЕ СТРАТЕГИИ

**Overtrading:**
- `try_set_order_in_flight()` — блокирует второй вход пока первый в работе ✅
- `is_in_position()` check — блокирует buy/sell при открытой позиции ✅
- Нет per-bar signal cooldown — стратегия может генерировать open-close-open-close каждый бар ⚠️
   **Статус: СОГЛАСЕН.** Специального cooldown/частотного лимитера на уровне стратегии я в core не нашёл.

**Циклы:**
- Нет защиты от rapid cycling (buy → bar → close → bar → buy → ...). RiskGuard не трекит trade frequency.
   **Статус: СОГЛАСЕН.** `RiskGuard` действительно не содержит счётчиков частоты сделок или cooldown по времени.

---

### 32. 🔁 ГЛОБАЛЬНАЯ ИДЕМПОТЕНТНОСТЬ

| Операция | Идемпотентна? | Механизм |
|----------|--------------|----------|
| `record_fill()` | ⚠️ Частично | `_seen_fills` check (TOCTOU) |
| `save_order()` | ✅ Да | `exec_key` dedup in write_lock |
| `start_live_engine()` | ✅ Да | `_launching_engines` + `_live_engines` |
| `stop_live_engine()` | ✅ Да | engine removed from dict atomically |
| `execute_signal()` | ✅ Да | submission_key + order_in_flight |
| `reconcile()` | ✅ Да | interval-gated, no side effects |

**Статус: СОГЛАСЕН.** Таблица корректно отмечает `record_fill()` как частично идемпотентный, а не полностью безопасный.

---

### 33. 📊 OBSERVABILITY

**Метрики ([runtime_metrics.py](core/runtime_metrics.py), [observability.py](core/observability.py)):**
- `emit_audit_event()` — audit trail ✅
- `snapshot()` — latencies, counters, audit_events ✅
- `collect_strategies_health()` — per-strategy health ✅
- `collect_runtime_metrics()` — drift, latency, counters ✅
- `collect_health_snapshot()` — connectors, strategies, pending orders ✅

**Алерты:**
- Telegram notifications: STRATEGY_CRASHED, mismatch alerts, late fills ✅
- Health server endpoint: periodic health check ✅
- **Нет ntfy / PagerDuty / webhook integration** (кроме ntfy_notifier.py — существует)
   **Статус: ЧАСТИЧНО СОГЛАСЕН.** Готовой интеграции PagerDuty/webhook я не вижу, а `ntfy_notifier.py` в дереве есть. То есть про отсутствие полноценной интеграции тезис разумный, но формулировка уже сама содержит оговорку.

---

### 34. 🧩 ЧАСТИЧНЫЕ СБОИ

| Сбой | Поведение | Оценка |
|------|----------|--------|
| Connector disconnect | Watchdog stops engines, resync on reconnect | ✅ |
| get_history timeout | Consecutive counter → degraded state | ✅ |
| DLL callback failure | Exception logged, state preserved | ✅ |
| Storage write failure | Atomic write (.tmp → rename), fallback to backups | ✅ |
| Telegram unavailable | Exception caught, trading continues | ✅ |
| health_server down | Exception caught, app continues | ✅ |
| Strategy on_bar() crash | Exception caught in `_process_bar()`, logged, continues | ✅ |
| Order monitor crash | Exception caught, order_in_flight may remain set | ⚠️ |

**Статус: ГАЛЮЦИНАЦИЯ.** Для рыночного monitor-path текущий код в типовых ветках очищает `order_in_flight`, а критичные вызовы обёрнуты достаточно плотно. Теоретически аварийный крэш потока всегда возможен, но заявленный пункт как установленный дефект по текущему коду не подтверждаю.

---

## 🎯 СПЕЦИАЛЬНЫЙ БЛОК: «Закрыть все позиции по стратегии»

### Реализация: [core/strategy_flatten.py](core/strategy_flatten.py)

**Класс:** `StrategyFlattenExecutor`

**Логика:**
1. `build_strategy_flatten_plan()` — строит план на основе `strategy_position_book` ✅
2. `_validate_broker_position()` — проверяет у broker: side совпадает, qty достаточен ✅
3. `connector.close_position_result()` — отправляет market close ✅
4. При `wait_for_confirmation=True`:
   - `_flatten_until_target()` — retry loop с `max_child_orders=3` ✅
   - `_wait_for_terminal_order()` — poll order status ✅
   - `_wait_for_target()` — poll position book ✅

**Закрывает ли ВСЕ позиции?**
- `get_strategy_position_book()` возвращает все открытые lots по стратегии ✅
- Loop по всем items в плане ✅

**Частичные fills:**
- `_flatten_until_target()` повторяет close order если `current_qty > target_qty` ✅
- Максимум 3 child orders (защита от бесконечного loop) ✅
- При неуспехе → `manual_intervention_required` ✅

**Дублирование ордеров:**
- Последовательное исполнение (один item за раз) ✅
- `_validate_broker_position()` перед каждым ордером перепроверяет position у broker ✅
- Нет parallel close → нет дублей ✅

**Зависшие позиции:**
- При timeout → `manual_intervention_required` status ✅
- При `broker_position_not_found` → `manual_intervention_required` ✅
- Audit events для всех исходов ✅

**Оценка:** Хорошо реализован. Основной risk: broker_qty_below_strategy_qty может блокировать close, но это правильное поведение (position book расположен с биржей).

---

## 🔬 СВОДНАЯ ТАБЛИЦА ISSUES

**Статусы по сводным issue-кодам:**
- **RC-1 — СОГЛАСЕН.** Это тот же FillLedger TOCTOU.
- **RC-2 — СОГЛАСЕН.** Это тот же daily loss race.
- **RC-3 — ЧАСТИЧНО СОГЛАСЕН.** Защита от дубля reconnect есть, но не атомарная.
- **RC-4 — СОГЛАСЕН.** Read/write lock discipline по `_last_bar_dt` нарушена.
- **LC-1 — СОГЛАСЕН.** `update_position()` не чистит `order_in_flight`.
- **LC-2 — СОГЛАСЕН.** STALE_STATE может блокировать submission key без TTL.
- **LC-3 — СОГЛАСЕН.** Stale reservations не удаляются.
- **CA-1 — СОГЛАСЕН.** Комиссии считаются на float.
- **CA-2 — СОГЛАСЕН.** Drawdown нормализуется на текущий qty.
- **CA-3 — СОГЛАСЕН.** Equity flush даёт накопительный O(n²).
- **AR-1 — ЧАСТИЧНО СОГЛАСЕН.** Ошибка остатка в chase fallback есть, но worst-case описан жёстче, чем ведёт себя текущая ветка.
- **AR-2 — СОГЛАСЕН.** `manual_close_position()` one-shot.
- **AR-3 — ГАЛЮЦИНАЦИЯ.** Plaintext fallback для DPAPI в текущем коде не подтверждается.

### Race Conditions

| Issue | Файл | Строки | Severity | Описание |
|-------|------|--------|----------|----------|
| RC-1 | fill_ledger.py | ~127-182 | 🔴 CRITICAL | TOCTOU в дедупликации fill_id |
| RC-2 | risk_guard.py | ~110-141 | 🟠 HIGH | Daily loss limit не под lock |
| RC-3 | base_connector.py | ~344-356 | 🟡 MEDIUM | Multiple reconnect threads |
| RC-4 | live_engine.py | ~605-618 | 🟡 LOW | _last_bar_dt read outside lock |

### Lifecycle Ошибки

| Issue | Файл | Строки | Severity | Описание |
|-------|------|--------|----------|----------|
| LC-1 | position_tracker.py | ~117-121 | 🟠 HIGH | update_position() не чистит order_in_flight |
| LC-2 | order_executor.py | ~349,769 | 🟠 HIGH | STALE_STATE блокирует submission навсегда |
| LC-3 | reservation_ledger.py | ~131-145 | 🟠 HIGH | Stale резервы не удаляются |

### Ошибки расчётов

| Issue | Файл | Строки | Severity | Описание |
|-------|------|--------|----------|----------|
| CA-1 | commission_manager.py | ~143-168 | 🟡 MEDIUM | Float arithmetic вместо Decimal |
| CA-2 | equity_tracker.py | ~79-83 | 🟡 MEDIUM | DD нормализация на переменный qty |
| CA-3 | trade_recorder.py | ~126-166 | 🟡 MEDIUM | O(n²) get_total_pnl при каждом flush |

### Архитектурные проблемы

| Issue | Файл | Severity | Описание |
|-------|------|----------|----------|
| AR-1 | order_placer.py ~187 | 🔴 CRITICAL | Chase fallback не проверяет partial fills |
| AR-2 | live_engine.py ~447 | 🟡 MEDIUM | manual_close без retry |
| AR-3 | storage.py ~277 | 🟡 MEDIUM | DPAPI failure → plaintext secrets |

---

## 🛠 ПЛАН РЕФАКТОРИНГА

**Статусы по пунктам плана:**
1. **СОГЛАСЕН.** Исправляет реальный TOCTOU в FillLedger.
2. **ЧАСТИЧНО СОГЛАСЕН.** Исправление полезное, но исходный worst-case в audit описан жёстче, чем ведёт себя текущая fallback-ветка.
3. **СОГЛАСЕН.** Это точечный фикс подтверждённого race в daily loss.
4. **СОГЛАСЕН.** Исправляет подтверждённое зависание `order_in_flight`.
5. **СОГЛАСЕН.** TTL для blocked keys нужен по текущей логике.
6. **СОГЛАСЕН.** Cleanup stale reservations нужен.
7. **СОГЛАСЕН.** Atomарная защита запуска reconnect thread имеет смысл.
8. **СОГЛАСЕН.** Retry/alert для manual close закроет реальный one-shot gap.
9. **СОГЛАСЕН.** В UI stop-confirm сейчас отсутствует.
10. **СОГЛАСЕН.** Time-based debounce сейчас не реализован.
11. **ЧАСТИЧНО СОГЛАСЕН.** Для полного close пункт завышен, но для partial close stale-qty риск есть.
12. **СОГЛАСЕН.** Возврат `0` на exception в history_qty действительно маскирует ошибки.
13. **СОГЛАСЕН.** Закроет подтверждённый performance-pattern O(n²).
14. **СОГЛАСЕН.** Перевод расчёта комиссии на Decimal технически обоснован.
15. **ЧАСТИЧНО СОГЛАСЕН.** Versioning нужен для runtime JSON, хотя в secret-store version marker уже есть.
16. **СОГЛАСЕН.** Aggregate reconcile действительно отсутствует.
17. **ЧАСТИЧНО СОГЛАСЕН.** Это хорошая оптимизация, но сам bottleneck по worker-count в audit оценочный.
18. **СОГЛАСЕН.** Guard по trade frequency в core сейчас отсутствует.
19. **ГАЛЮЦИНАЦИЯ.** Hard fail по DPAPI фактически уже есть; plaintext fallback в текущем коде не подтверждается.

### Фаза 1: СРОЧНЫЕ ФИКСЫ (P0, 1-2 дня)

1. **FillLedger TOCTOU** — переместить `_seen_fills[fill_id]` добавление ВНУТРЬ первого lock-блока, до проекции
2. **Chase fallback** — проверять `chase.filled_qty` перед fallback market order; `remaining = qty - chase.filled_qty`
3. **Daily loss limit lock** — обернуть block в `with self._lock`

### Фаза 2: СТАБИЛИЗАЦИЯ (P1, 3-5 дней)

4. **order_in_flight cleanup** — в `update_position()` при position==0 сбрасывать `_order_in_flight`
5. **STALE_STATE key TTL** — добавить TTL для `_blocked_submission_keys` (5 минут)
6. **Stale reservations cleanup** — удалять stale резервы через `stale_cleanup_timeout`
7. **Reconnect thread guard** — Lock на `_reconnect_thread` в BaseConnector
8. **manual_close retry** — 2-3 попытки с backoff (1с, 3с)

### Фаза 3: LIFECYCLE (P2, 1 неделя)

9. **Stop confirmation** — добавить Yes/No dialog перед stop agent в UI
10. **Button debounce** — debounce 500ms для кнопок close/stop
11. **Stale position qty** — перечитывать qty при нажатии кнопки close, не при рендере
12. **History divergence** — `_get_history_qty()` бросать exception вместо return 0

### Фаза 4: АРХИТЕКТУРА (P3, 2 недели)

13. **Incremental realized PnL** — кэшировать кумулятивный PnL, обновлять при каждой записи пары
14. **CommissionManager Decimal** — перевести на Decimal арифметику
15. **Schema versioning** — добавить version marker в JSON файлы
16. **Aggregate reconcile** — Σ positions(strategies) vs broker total

### Фаза 5: ОПТИМИЗАЦИЯ (P4)

17. **Monitor pool sizing** — dynamic sizing по количеству стратегий
18. **Trade frequency guard** — max trades per hour per strategy
19. **DPAPI hard fail** — не записывать plaintext при DPAPI failure

---

## 🧪 10 WORST-CASE СЦЕНАРИЕВ

**Статусы по сценариям:**
1. **ЧАСТИЧНО СОГЛАСЕН.** Основан на реальном дефекте fallback-логики, но сам сценарий overstated для текущей ветки `filled_qty == 0`.
2. **СОГЛАСЕН.** Это прямое следствие TOCTOU в FillLedger.
3. **СОГЛАСЕН.** Полностью соответствует зависанию `order_in_flight` после `update_position()`.
4. **СОГЛАСЕН.** Это прямой worst-case подтверждённого race в daily loss.
5. **СОГЛАСЕН.** Логически вытекает из отсутствия cleanup stale reservations.
6. **СОГЛАСЕН.** Бессрочная блокировка submission key подтверждена.
7. **СОГЛАСЕН.** Это естественный worst-case one-shot `manual_close_position()`.
8. **ЧАСТИЧНО СОГЛАСЕН.** Reconnect race есть, но сценарий с множеством параллельных потоков описан агрессивнее, чем текущая частичная защита.
9. **СОГЛАСЕН.** Это прямое развитие подтверждённого O(n²) equity flush.
10. **СОГЛАСЕН.** Gap между durable projection и in-memory mark действительно существует.

### 1. Double Chase Fill
```
ChaseOrder fills 5/10 contracts → timeout → fallback market 10 more
→ TOTAL: 15 contracts open (expected 10)
→ LOSS: oversized position in volatile market
```

### 2. FillLedger Double Write
```
Two threads: record_fill(same_exec_id) concurrently
→ Both pass dedup check, both write to order_history
→ PnL doubled, phantom position
```

### 3. Frozen Strategy (order_in_flight stuck)
```
Order placed → connector crash → reconnect → reconciler update_position(0,0,0)
→ But order_in_flight still True
→ Strategy never trades again until manual restart
```

### 4. Daily Loss Bypass at Midnight
```
Thread A and B both check check_risk_limits() at 00:00:00
→ Both see today != _today_date, both reset baseline
→ One uses stale baseline → loss limit bypassed
```

### 5. Capital Lock (Stale Reservations)
```
10 failed orders → 10 stale reservations × 100K each
→ total_reserved = 1M → no capital for new trades
→ Strategy generates signals but all rejected
```

### 6. STALE_STATE Permanent Block
```
place_order → STALE_STATE response (ambiguous)
→ submission_key blocked → same signal pattern never executes again
→ Strategy inoperable on this ticker until restart
```

### 7. Emergency Close Network Failure
```
Operator clicks "Close position" → network timeout
→ Returns "close_failed", no retry
→ Position open over weekend/holiday
→ Gap down Monday → large loss
```

### 8. Multiple Reconnect Threads
```
Rapid disconnect-reconnect-disconnect cycle
→ 3 reconnect threads running simultaneously
→ All call connect() → connector in inconsistent state
→ Duplicate orders possible
```

### 9. Equity O(n²) Slowdown
```
Day 1: 100 trades, flush_equity takes 10ms each = 1s total
Day 30: 3000 trades, flush_equity takes 300ms each = 15 minutes total
→ Signal processing delayed → missing entry/exit points
```

### 10. Audit Gap on Crash
```
System crashes between FillLedger projection and _seen_fills update
→ Fill written to order_history but not in _seen_fills
→ On restart: fill_id not in memory → duplicate NOT detected
→ Same fill recorded again → double PnL
```

---

## ✅ СПИСОК ИНВАРИАНТОВ

```
INV-1: ∀ strategy: engine.position_qty == broker.position_qty (verified every 60s)
INV-2: ∀ strategy: order_in_flight == True ⟹ at most 1 active order
INV-3: ∀ strategy: position != 0 ⟹ try_set_order_in_flight() returns False for open
INV-4: ∀ pair: net_pnl = gross_pnl - entry_commission - exit_commission
INV-5: ∀ equity: equity = realized_pnl + unrealized_pnl
INV-6: ∀ fill_id: recorded at most once in order_history (via exec_key dedup)
INV-7: ∀ strategy: only manual action can close positions (no auto-close)
INV-8: ∀ (account, ticker): at most 1 strategy owner (ownership registry)
INV-9: ∀ order: lifecycle transitions are monotonic (terminal is forever, except late_fill_repair)
INV-10: ∀ bar: processed only once (last_bar_dt monotonicity check)
```

---

## 🔒 БЕЗОПАСНАЯ МОДЕЛЬ LIFECYCLE

```
┌─────────────────────────────────────────────────────────────┐
│                    STARTUP                                   │
│                                                              │
│  1. cleanup_orphan_tmp()        ← Recovery                  │
│  2. register_connectors()       ← Deferred init             │
│  3. autoconnect_connectors()    ← Schedule-gated            │
│  4. autostart_strategies()      ← Threaded, schedule-gated   │
│     for each strategy:                                       │
│     a. wait_for_connector(30s)                               │
│     b. claim_ownership(account, ticker)                      │
│     c. LiveEngine(...)                                       │
│     d. startup_preflight()      ← _detect_position()        │
│     e. call_on_start()          ← User hook                 │
│     f. engine.start()           ← _poll_loop thread         │
│  5. start_engine_watchdog(15s)  ← Monitor connectors        │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                    TRADING                                    │
│                                                              │
│  _poll_loop:                                                 │
│    wait(poll_interval)                                       │
│    _load_and_update()           ← get_history + validate     │
│    reconciler.reconcile()       ← engine↔broker↔history      │
│    if new_bar:                                               │
│      on_precalc(df)                                          │
│      signal = on_bar(bars, position)                         │
│      if signal:                                              │
│        PRE-TRADE GATE:                                       │
│          circuit_breaker?                                     │
│          risk_limits?                                         │
│          sync_status == synced?                               │
│          try_set_order_in_flight()?                           │
│        execute_signal()                                      │
│        MONITOR: poll order status → record_trade             │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                    SHUTDOWN                                   │
│                                                              │
│  SAFETY: stop() НИКОГДА не закрывает позицию автоматически   │
│                                                              │
│  1. stop_live_engine(strategy_id)                            │
│     a. engine.stop()            ← set _stop_event            │
│     b. order_executor.stop()    ← cancel active chases       │
│     c. call_on_stop()           ← User hook                 │
│     d. release_ownership()                                   │
│     e. equity flush                                          │
│  2. WARNING if position != 0   ← Log + operator notice       │
│                                                              │
│  MANUAL CLOSE (operator action):                             │
│  3. manual_close_position()                                  │
│     → place market order                                     │
│     → NO auto-retry (operator must verify)                   │
│                                                              │
│  STRATEGY FLATTEN (operator action):                         │
│  4. StrategyFlattenExecutor.execute()                        │
│     → validate broker position                               │
│     → close_position_result()                                │
│     → wait for confirmation / manual_intervention             │
└─────────────────────────────────────────────────────────────┘
```

---

## ✅ ПОДТВЕРЖДЕНИЕ: НЕТ АВТОМАТИЧЕСКОГО ЗАКРЫТИЯ ПОЗИЦИЙ

**Статус: СОГЛАСЕН.** Этот блок соответствует текущему коду: автоматического закрытия позиций по stop/degraded/circuit-breaker paths я не нашёл, а ручные пути закрытия явно выделены отдельно.

**Проверено:**
1. `LiveEngine.stop()` — явный комментарий «НИКОГДА не закрывает позицию» ✅
2. `_emergency_close_position()` — **НЕ СУЩЕСТВУЕТ** в текущем коде ✅
3. Circuit breaker — блокирует новые ордера, **не закрывает** позиции ✅
4. Timeout/degraded — блокирует open signals, **не закрывает** ✅
5. Watchdog disconnect — останавливает engine, **не закрывает** позиции ✅
6. `main.py` exit — flush equity, **не закрывает** позиции ✅

**Единственные пути закрытия позиций (все ручные):**
- `manual_close_position()` — через UI кнопку ✅
- `StrategyFlattenExecutor.execute()` — через UI кнопку ✅
- `PositionManager.close_position()` — через UI кнопку ✅
- Стратегия `on_bar()` → signal `action="close"` — по решению стратегии (ожидаемое поведение)

---

*Конец аудита. Статусы по спорным и повторяющимся пунктам проставлены по текущему коду.*
