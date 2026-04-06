# План рефакторинга и исправлений по cc_review.md

Этот план сводит повторяющиеся пункты из cc_review.md в один backlog по уникальным проблемам.
Если одна и та же проблема встречалась в TOP-10, секциях, сводной таблице, worst-case и плане рефакторинга, она оформлена как один TASK.

Легенда текущего статуса выполнения TASK:
- Не начато: задача подтверждена или частично подтверждена, код ещё не менялся.
- Не начато, нужен scope-fix: проблема реальна, но формулировка review завышена, поэтому перед реализацией уже уточнён точный объём.
- Не начато, отложено: задача не срочная, подтверждена лишь частично и сознательно перенесена ниже по приоритету.
- Закрыто без реализации: пункт из review признан галлюцинацией, код менять не нужно.

## P0

### TASK-001 — FillLedger TOCTOU в дедупликации fill_id
- Проблема: РИСК 1, BLOCKER-1, RC-1, сценарий Double Write. В FillLedger один и тот же fill может пройти параллельную проверку и быть записан дважды.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: проверка fill_id выполняется под lock, затем lock отпускается, выполняется durable-проекция в order_history и trades_history, и только потом fill_id помечается в _seen_fills. Между check и mark есть реальное окно TOCTOU.
- Путь решения проблемы: ввести атомарное резервирование fill_id до I/O. Под одним lock сначала помечать fill_id как processing, затем выполнять проекцию, после успешной durable-записи переводить состояние в committed, а при ошибке снимать processing-метку, чтобы разрешить безопасный retry. Durable dedup по exec_key оставить второй линией защиты.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-002 — Chase fallback должен отправлять только остаток объёма
- Проблема: РИСК 2, BLOCKER-2, AR-1, сценарий Double Chase Fill. Fallback-ветка chase использует исходный qty вместо остатка.
- Статус из cc_review.md: ЧАСТИЧНО СОГЛАСЕН.
- Объяснение статуса из cc_review.md: проблема с расчётом remaining реальна, но текущая fallback-ветка срабатывает только при chase.filled_qty == 0, поэтому описанный worst-case про partial fill и затем full-size market завышен.
- Путь решения проблемы: после завершения chase всегда рассчитывать authoritative remaining_qty из фактически исполненного объёма после cancel-and-wait. Если remaining_qty > 0, отправлять market только на remaining_qty; если remaining_qty == 0, fallback не делать. Эту логику держать в одном месте внутри place_chase.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-003 — Stale reservations должны очищаться автоматически
- Проблема: РИСК 3, LC-3, сценарий Capital Lock. Stale-резервы остаются в ReservationLedger и продолжают блокировать капитал.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: _mark_stale() только ставит stale-флаг и stale_reason, но не удаляет запись и не исключает её из total_reserved().
- Путь решения проблемы: добавить lifecycle stale-reservation из двух фаз: mark_stale для краткого окна расследования и последующий deterministic cleanup по timeout с audit event. total_reserved() должен суммировать только активные резервы, а stale-записи после cleanup переносить в audit/log, а не держать в рабочем ledger.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-004 — Daily loss rollover должен быть полностью атомарным
- Проблема: РИСК 4, RC-2, сценарий Daily Loss Bypass at Midnight. Смена дня и baseline metric в RiskGuard обновляются без lock.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: _today_date и _baseline_metric обновляются вне self._lock, поэтому два потока могут одновременно переинициализировать baseline и получить неконсистентный daily_pnl.
- Путь решения проблемы: вынести весь daily-loss блок в отдельный метод RiskGuard и выполнять вычисление today, baseline rollover и daily_pnl внутри одного критического сектора под self._lock.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-005 — Ошибки расчёта daily loss нельзя глушить pass
- Проблема: секция Risk Management. Ошибка в расчёте daily loss сейчас обходится через except Exception: pass, что может пропустить лимит.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: если получение equity/PnL или расчёт baseline падает, метод молча продолжает разрешать торговлю.
- Путь решения проблемы: заменить fail-open на fail-safe. При ошибке daily-loss расчёта переводить стратегию в manual_intervention_required или возвращать блокировку новых входов с явной причиной и audit event.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-006 — Sync path должен сбрасывать transient order state
- Проблема: РИСК 5, LC-1, сценарий Frozen Strategy. В core/position_tracker.py метод update_position() около L138 не очищает order_in_flight после authoritative sync.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: после reconcile можно получить flat state, но с зависшим _order_in_flight=True, из-за чего новые сигналы навсегда отвергаются; это относится именно к core/position_tracker.py:update_position(), а не к strategy_position_book.py, который остаётся read-only view.
- Путь решения проблемы: в core/position_tracker.py выделить явный sync-path API для authoritative broker update и внутри него атомарно обновлять position/qty/entry_price и очищать transient order state. Минимальный безопасный шаг: исправить update_position() около L138, чтобы sync-path не сохранял зависший _order_in_flight.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-007 — STALE_STATE block keys должны протухать
- Проблема: РИСК 6, LC-2, сценарий STALE_STATE Permanent Block. _blocked_submission_keys живёт без TTL.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: submission key добавляется в set и автоматически не удаляется, кроме успешных release-path или stop/restart.
- Путь решения проблемы: заменить set на registry с timestamp и reason. Ключ должен блокироваться на ограниченный TTL и дополнительно сниматься после успешного reconcile, подтверждающего отсутствие pending-ордера у брокера.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

## P1

### TASK-008 — _last_bar_dt должен читаться и писаться под одним lock
- Проблема: РИСК 7, RC-4. У LiveEngine нарушена lock-дисциплина по _last_bar_dt.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: чтение _last_bar_dt есть вне _bars_lock, а запись идёт под _bars_lock, что даёт stale read при расширении concurrency.
- Путь решения проблемы: спрятать _last_bar_dt за маленький private API get_last_bar_dt()/set_last_bar_state() и делать все чтения и записи только под _bars_lock.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-009 — Manual close нужен bounded retry и явная эскалация
- Проблема: РИСК 8, AR-2, сценарий Emergency Close Network Failure. В close-path ядра вокруг core/order_executor.py и вызова connector.close_position_result() около L833 закрытие выполняется одной попыткой без recovery-policy.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: close-path использует одиночный вызов close_position_result()/close submit без bounded retry, поэтому при сетевой/транспортной ошибке оператор получает fail без встроенного recovery.
- Путь решения проблемы: централизовать retry-policy в close execution path внутри core/order_executor.py, рядом с вызовом connector.close_position_result() около L833: ограниченное число повторов, короткий backoff и обязательная эскалация в manual intervention после исчерпания попыток.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-010 — Запуск reconnect thread должен быть атомарным
- Проблема: РИСК 9, RC-3. В core/base_connector.py метод start_reconnect_loop() около L373 частично защищён, но проверка is_alive() не атомарна.
- Статус из cc_review.md: ЧАСТИЧНО СОГЛАСЕН.
- Объяснение статуса из cc_review.md: базовая защита от повторного запуска уже есть, но между is_alive() и стартом нового thread в core/base_connector.py остаётся подтверждённое race window.
- Путь решения проблемы: в core/base_connector.py добавить отдельный reconnect_start_lock и выполнять check-and-start внутри одного критического сектора прямо в start_reconnect_loop().
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-011 — Realized PnL для equity flush нужно сделать инкрементальным
- Проблема: РИСК 10, CA-3, сценарий Equity O(n²) Slowdown. Цепочка core/trade_recorder.py:_flush_equity() -> core/order_history.py:get_total_pnl() -> core/order_history.py:get_order_pairs() пересобирает полный FIFO state на каждой сделке.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: O(n²) подтверждён именно этой цепочкой: trade_recorder запускает flush после каждой сделки, get_total_pnl заново вызывает get_order_pairs, а тот пересобирает весь FIFO из истории.
- Путь решения проблемы: оставить trade_recorder.py только потребителем realized snapshot, а инкрементальный расчёт realized_pnl вынести в слой order history/accounting. Полный rebuild через get_order_pairs() использовать только для recovery/reindex path.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-012 — Freshness нельзя полностью опирать на локальные часы
- Проблема: секция Latency и Timing. Проверки stale опираются на локальное время процесса.
- Статус из cc_review.md: ЧАСТИЧНО СОГЛАСЕН.
- Объяснение статуса из cc_review.md: явной синхронизации с серверным временем нет; это hardening-gap, а не доказанный production-баг.
- Путь решения проблемы: сохранить local clock как primary источник freshness decision, а source_ts/receive_ts использовать только для cross-validation, drift alert и диагностики аномалий timestamps со стороны коннектора/биржи.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

## P2

### TASK-013 — Quote staleness policy должна учитывать рыночную фазу
- Проблема: секция Market Data Integrity. Единый stale_quote_budget_ms может отклонять валидные сигналы в аукционах и pre-clearing.
- Статус из cc_review.md: ЧАСТИЧНО СОГЛАСЕН.
- Объяснение статуса из cc_review.md: риск реален, но зависит от режима торгов и настроек, а не является универсальным багом.
- Путь решения проблемы: не строить полноценную schedule-aware policy на первом шаге. Начать с простого phase whitelist: явный список торговых фаз/окон, в которых stale-check смягчается или отключается, без полной модели расписания MOEX.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-014 — cancel_order в limit-price monitor должен быть изолирован от зависания коннектора
- Проблема: секция Order Management. cancel_order() вызывается напрямую без timeout guard.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: если коннектор повиснет внутри cancel_order(), monitor thread зависнет.
- Путь решения проблемы: выполнять cancel_order через отдельный bounded worker/future с timeout и переводить ордер в manual_intervention_required, если cancel ack не получен вовремя.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-015 — Retry при persistent place_order error в Chase должен иметь backoff
- Проблема: секция Order Management. В chase retry есть фиксированная пауза 1 секунда без backoff.
- Статус из cc_review.md: ЧАСТИЧНО СОГЛАСЕН.
- Объяснение статуса из cc_review.md: бесконечность в review завышена, но отсутствие backoff на persistent error подтверждается.
- Путь решения проблемы: перейти на capped exponential backoff внутри chase placement loop, сохранив общий timeout как верхнюю границу жизни chase.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-016 — Max drawdown нужно считать как абсолютную метрику equity, а не нормализовать на текущий qty
- Проблема: CA-2, секция PnL. max_drawdown зависит от текущего размера позиции.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: drawdown в equity_tracker делится на текущий position_qty, поэтому метрика меняется вместе с объёмом позиции.
- Путь решения проблемы: хранить первичной метрикой абсолютный max_drawdown по equity curve. Если нужна нормализация на единицу объёма, считать её отдельной secondary-метрикой, а не переопределять основную DD.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-017 — Комиссии нужно перевести на Decimal end-to-end
- Проблема: CA-1, секции Комиссии и Численная точность. CommissionManager.calculate() работает на float.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: сама проблема точности небольшая, но кодовая база в финансовом ядре уже ориентирована на Decimal.
- Путь решения проблемы: перевести весь расчёт комиссии внутри CommissionManager на Decimal и конвертировать к float только на storage/UI boundary.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-018 — Получение MOEX тарифов должно быть автоматизировано
- Проблема: секция Комиссии. Ставки MOEX по сути поддерживаются вручную, автоматического обновления нет.
- Статус из cc_review.md: ЧАСТИЧНО СОГЛАСЕН.
- Объяснение статуса из cc_review.md: в проекте уже есть заготовки для обновления, поэтому тезис про полностью статичный JSON устарел, но фоновой автоматизации действительно нет.
- Путь решения проблемы: добавить один регламентированный scheduled job для обновления тарифов с валидацией входных данных, записью last_update и fallback на предыдущий конфиг при ошибке.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-019 — Reconciler не должен подменять ошибку history_qty значением 0
- Проблема: секция Reconciliation, Phase 3 пункт 12. _get_history_qty() возвращает 0 при exception.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: это создаёт ложный mismatch с broker и маскирует источник проблемы.
- Путь решения проблемы: вернуть из _get_history_qty() structured failure вверх по стеку и вводить отдельный статус reconcile вроде history_unavailable вместо подстановки 0.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-020 — Self-heal в reconciler нужен с порогом и cooldown
- Проблема: секция Reconciliation. Self-heal запускается при каждом mismatch.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: alert уже имеет cooldown, а self-heal выполняется на каждом цикле без порога по количеству рассинхронов.
- Путь решения проблемы: добавить mismatch streak counter и отдельный self-heal cooldown. Лечить состояние только после N подряд mismatch и не чаще одного heal за заданный интервал.
- Текущий статус выполнения TASK: Не начато.

### TASK-021 — RiskGuard должен поддерживать per-instrument limits
- Проблема: секция Risk Management. Сейчас risk limits есть на strategy/account-level, но нет per-instrument ограничений.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: review прямо отмечает отсутствие per-instrument risk controls.
- Путь решения проблемы: добавить в конфигурацию стратегии/аккаунта per-instrument risk profile и проверять его в pre-trade gate до placement.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-022 — Нужен trade-frequency guard против rapid cycling
- Проблема: секции Поведение стратегии и план Phase 5. Нет ограничений на частоту сделок и per-bar cooldown.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: RiskGuard не трекает частоту сделок, поэтому стратегия может генерировать rapid open-close-open cycles.
- Путь решения проблемы: встроить в RiskGuard одно окно частоты сделок с параметрами max_trades_per_window и cooldown_after_close, проверяемыми в pre-trade gate.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-023 — Остановка стратегии из UI должна требовать подтверждение
- Проблема: секции Human Errors, UI/UX, план Phase 3. Кнопка stop не требует подтверждения.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: strategy_window вызывает _stop_strategy() напрямую, поэтому оператор может сиротить открытую позицию одним кликом.
- Путь решения проблемы: добавить confirm-dialog перед stop action в strategy_window и пропустить сам stop через ту же safety-модель, что и destructive close actions.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-024 — Partial close должен брать актуальный qty в момент клика
- Проблема: секция UI/UX, пункт stale qty. Для partial close qty захватывается при рендере кнопки.
- Статус из cc_review.md: ЧАСТИЧНО СОГЛАСЕН.
- Объяснение статуса из cc_review.md: для полного close claim завышен, но для partial close stale-risk действительно остаётся.
- Путь решения проблемы: перед открытием PartialCloseDialog заново читать текущую позицию из PositionManager/strategy position book и использовать её как единственный источник max_qty.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-025 — Для destructive buttons нужен time-based debounce
- Проблема: секция UI/UX, план Phase 3. Есть DestructiveActionGuard, но нет debounce по времени.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: жёсткого debounce нет, guard только уменьшает риск дублей во время выполнения action.
- Путь решения проблемы: расширить DestructiveActionGuard до общей модели safety + debounce window и использовать этот единый guard для stop/close/flatten кнопок.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-026 — QUIK connector нужен отдельный нормальный набор тестов
- Проблема: секция Testability. Покрытие QUIK заметно слабее остального ядра.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: в текущем дереве виден только один test_quik_connector_contract.py.
- Путь решения проблемы: добавить connector-level contract suite для QUIK с моками на connect, positions, order status, history, reconnect и error paths.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-027 — UI safety flows нужно покрыть PyQt smoke tests
- Проблема: секция Testability. UI tests почти отсутствуют.
- Статус из cc_review.md: ЧАСТИЧНО СОГЛАСЕН.
- Объяснение статуса из cc_review.md: gap реальный, но численная оценка из review неточная.
- Путь решения проблемы: добавить небольшой, но стабильный набор PyQt smoke/integration tests на destructive confirmations, partial close dialog, stop dialog и action guard.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-028 — Strategy loader нужен unit test suite
- Проблема: секция Testability. Для strategy loader нет отдельного покрытия.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: отдельных тестов на discovery, import failures и schema extraction не найдено.
- Путь решения проблемы: добавить unit suite на discovery, metadata extraction, param schema parsing, import errors и защиту от side effects при загрузке.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-029 — Нужна централизованная schema-driven validation параметров стратегии
- Проблема: секции Human Errors и Configuration. Валидация параметров сейчас зависит от on_start() конкретной стратегии.
- Статус из cc_review.md: ЧАСТИЧНО СОГЛАСЕН.
- Объяснение статуса из cc_review.md: централизованной обязательной схемы валидации в core нет.
- Путь решения проблемы: внедрить единый validator на основе strategy param schema и проверять параметры до запуска стратегии, а не перекладывать это на пользовательский on_start().
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-030 — Runtime JSON нужны schema version и migration registry
- Проблема: секция Versioning, Phase 4 пункт 15. Для основных runtime JSON нет явной схемы миграции.
- Статус из cc_review.md: ЧАСТИЧНО СОГЛАСЕН.
- Объяснение статуса из cc_review.md: для secrets-store version marker уже есть, но для runtime-файлов model version действительно отсутствует.
- Путь решения проблемы: добавить version field в каждый runtime JSON и централизованный migration registry, вызываемый на чтении перед использованием данных.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-031 — Исторический warmup должен рассчитываться от timeframe и lookback, а не от грубой эвристики
- Проблема: секция Historical Data. На cold start может не хватить баров, а days = max(lookback // 50, 5) может недобрать историю.
- Статус из cc_review.md: ЧАСТИЧНО СОГЛАСЕН.
- Объяснение статуса из cc_review.md: общий cold-start тезис оценочный, но underfetch для больших lookback реально подтверждён.
- Путь решения проблемы: вычислять required_days детерминированно из timeframe, lookback и safety factor, а после fetch проверять фактическое число закрытых баров и дозапрашивать историю до достижения минимума.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-032 — trades_history нельзя тихо обрезать без архивации
- Проблема: секция Logs and Audit. trades_history ограничен 10_000 записями и старые сделки удаляются.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: при append после достижения лимита хвост файла просто отрезается.
- Путь решения проблемы: заменить silent truncation на архивную ротацию в timestamped snapshot files с сохранением активного hot-file разумного размера.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-033 — Нужен aggregate reconcile по аккаунту поверх strategy-level reconcile
- Проблема: секции Invariants и план Phase 4. Нет проверки суммы позиций всех стратегий против брокерского total.
- Статус из cc_review.md: СОГЛАСЕН.
- Объяснение статуса из cc_review.md: aggregate reconcile явно отсутствует.
- Путь решения проблемы: добавить account-level reconciler, который периодически сверяет сумму strategy-owned positions с брокерской позицией по аккаунту/тикеру и эскалирует несовпадения.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

## P3

### TASK-034 — Размер monitor pool оставить фиксированным, вопрос масштабирования отложить
- Проблема: секция Performance. В review предложено динамически масштабировать _monitor_pool при max_workers=4.
- Статус из cc_review.md: ЧАСТИЧНО СОГЛАСЕН.
- Объяснение статуса из cc_review.md: фиксированное значение действительно есть, но для текущей архитектуры "один executor на стратегию" доказательств, что 4 worker-а недостаточно, нет.
- Путь решения проблемы: код сейчас не менять. Оставить max_workers=4 как разумный baseline и возвращаться к масштабированию только после появления фактических метрик saturation/queueing на production-нагрузке.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

### TASK-035 — Нужна внешняя alerting-интеграция уровня webhook/incident channel
- Проблема: секция Observability. Помимо Telegram нет полноценного incident routing.
- Статус из cc_review.md: ЧАСТИЧНО СОГЛАСЕН.
- Объяснение статуса из cc_review.md: ntfy_notifier.py есть, поэтому claim не абсолютный, но единого production-ready alert routing слоя нет.
- Путь решения проблемы: ввести один generic notification gateway с webhook transport и policy routing, а Telegram/ntfy оставить адаптерами этого gateway.
- Текущий статус выполнения TASK: ВЫПОЛНЕНО.

## Закрытые пункты review

### TASK-036 — Security claim про plaintext fallback DPAPI закрывается без кодовых изменений
- Проблема: AR-3 и секция Security утверждают, что при отказе DPAPI секреты пишутся в plaintext.
- Статус из cc_review.md: ГАЛЮЦИНАЦИЯ.
- Объяснение статуса из cc_review.md: текущий storage.py не делает plaintext fallback, а падает с исключением, поэтому проблема в source не подтверждается.
- Путь решения проблемы: код не менять. Исключить этот пункт из backlog и зафиксировать в документации, что политика хранения секретов fail-fast, а не fallback-to-plaintext.
- Текущий статус выполнения TASK: Закрыто без реализации.

### TASK-037 — PnL claim про exit_commission в unrealized PnL закрывается без кодовых изменений
- Проблема: в review это подано как проблема, хотя речь идёт об оценочной модели unrealized PnL.
- Статус из cc_review.md: ГАЛЮЦИНАЦИЯ.
- Объяснение статуса из cc_review.md: использование current_price для оценки exit_commission — ожидаемая mark-to-market аппроксимация, а не дефект.
- Путь решения проблемы: код не менять. Оставить текущую модель и при необходимости только документировать, что unrealized PnL содержит estimated exit costs.
- Текущий статус выполнения TASK: Закрыто без реализации.

### TASK-038 — Claim про float slicing комиссии закрывается без кодовых изменений
- Проблема: review утверждает, что partial commission slicing делается через float.
- Статус из cc_review.md: ГАЛЮЦИНАЦИЯ.
- Объяснение статуса из cc_review.md: в текущем коде slicing идёт через valuation_service.slice_commission() на Decimal.
- Путь решения проблемы: код не менять. Убрать этот пункт из backlog и оставить как false positive review.
- Текущий статус выполнения TASK: Закрыто без реализации.

### TASK-039 — Claim про order monitor crash и зависший order_in_flight закрывается без кодовых изменений
- Проблема: review считает, что при крэше monitor-path order_in_flight может остаться навсегда установленным.
- Статус из cc_review.md: ГАЛЮЦИНАЦИЯ.
- Объяснение статуса из cc_review.md: для типовых веток market-order monitor текущий код очищает order_in_flight, а заявленный дефект как установленный не подтверждён.
- Путь решения проблемы: код не менять. Не выделять отдельную bugfix-задачу; при желании позже можно добавить регрессионный тест, но это уже не исправление подтверждённого бага.
- Текущий статус выполнения TASK: Закрыто без реализации.

## Порядок реализации

1. Сначала выполнить TASK-001, TASK-003, TASK-004, TASK-005, TASK-006.
2. Затем закрыть ордерный и reconcile hardening: TASK-002, TASK-009, TASK-014, TASK-019, TASK-020.
3. Затем заняться финансовой и исторической точностью: TASK-011, TASK-016, TASK-018, TASK-031, TASK-032, TASK-033.
4. После стабилизации ядра перейти к UI safety и тестам: TASK-023, TASK-024, TASK-025, TASK-026, TASK-027, TASK-028.
5. Hardening и отложенные улучшения брать отдельно, когда появится фактическая нагрузочная/рыночная необходимость: TASK-012, TASK-013, TASK-034, TASK-035.
6. Hallucination-задачи TASK-036..TASK-039 не реализовывать, только держать как закрытые элементы аудита.