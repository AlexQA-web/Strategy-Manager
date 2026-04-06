# Runbook

## Phase 0 Safety Rules

Дата обновления: 2026-04-05

### Инварианты эксплуатации

- Остановка стратегии не закрывает рыночную позицию автоматически.
- Crash, circuit breaker и get_history timeout не отправляют close-ордер автоматически.
- Неуспешный close_position не делает fallback на встречный market order.
- Любой close_failed переводит стратегию в stale/manual intervention path и требует действий оператора.
- Если LiveEngine не смог стартовать, он не регистрируется в runtime-реестре.

### Что должен делать оператор

- При остановке стратегии с открытой позицией проверить реальную позицию у брокера и закрывать её только явным действием.
- При status stale или сообщении manual_intervention_required не перезапускать стратегию вслепую; сначала сверить брокерскую позицию и историю исполнений.
- При close_failed считать позицию потенциально открытой до отдельного подтверждения от брокера.
- При failed start считать стратегию неактивной, даже если desired state в storage остался включённым.

### Regression-команда Phase 0

c:/Users/Alex_qA/PycharmProjects/trading_manager/.venv/Scripts/python.exe -m pytest tests/test_live_engine.py tests/test_order_executor.py tests/test_autostart.py -q

## Phase 1 Strategy Flatten Rules

Дата обновления: 2026-04-05

### Инварианты эксплуатации

- Закрытие из окна стратегии использует strategy-owned position book, а не account aggregate как источник объёма.
- Strategy-level и account-level destructive actions разведены в UI и подтверждаются разными диалогами.
- Strategy flatten всегда отправляет close через agent_name=strategy_id, чтобы closing fill атрибутировался той же стратегии.
- При partial fill следующий child-close отправляется только после terminal статуса предыдущего close-ордера.
- Если broker snapshot противоречит strategy book, flatten останавливается в manual_intervention_required.

### Что должен делать оператор

- Для закрытия только одной стратегии использовать кнопку strategy-level закрытия в окне стратегии.
- Account-level close использовать только как явное действие по всему счёту.
- Если strategy flatten вернул manual_intervention_required, сверить broker position, order status и историю fills до повторной попытки.

### Regression-команда Phase 1

c:/Users/Alex_qA/PycharmProjects/trading_manager/.venv/Scripts/python.exe -m pytest tests/test_strategy_flatten.py tests/test_position_manager.py tests/test_order_history.py tests/test_live_engine.py tests/test_order_executor.py tests/test_autostart.py -q

## Phase 2 Startup And Runtime Truth

Дата обновления: 2026-04-05

### Инварианты эксплуатации

- Перед запуском poll-loop стратегия проходит preflight snapshot и initial reconcile.
- `desired_state` в storage отражает волю пользователя, `actual_state` живёт в runtime registry.
- UI обязан различать intent и runtime: `active` в storage не означает, что engine реально торгует.
- Watchdog на disconnect останавливает runtime, но не переписывает user intent.
- Startup может завершиться в `synced`, `degraded` или `failed_start`; только `trading` означает активный poll-loop.

### Что должен делать оператор

- Если `desired_state=active`, но runtime показывает `failed_start` или `degraded`, считать стратегию неготовой к торговле.
- После reconnect проверять, что runtime вернулся хотя бы в `synced`, а не только `active` в таблице.
- При `manual_intervention_required` или `stale` не считать watchdog-перезапуск достаточным до проверки брокера.

### Regression-команда Phase 2

c:/Users/Alex_qA/PycharmProjects/trading_manager/.venv/Scripts/python.exe -m pytest tests/test_autostart.py tests/test_live_engine.py -q

## Phase 3 Restart Recovery And Pending Orders

Дата обновления: 2026-04-06

### Инварианты эксплуатации

- LiveEngine подписывается на reconnect через subscribe_reconnect и снимает подписку при stop.
- Каждый market/limit ордер получает durable pending snapshot сразу после submit и обновляет его на каждом lifecycle transition.
- После рестарта startup_preflight сначала восстанавливает pending orders по durable registry и broker order status, а уже потом разрешает trading runtime.
- Если pending registry и broker status не согласуются, стратегия уходит в manual_intervention_required и не стартует в trading.
- Повторная отправка того же signal/action с тем же idempotency key после ambiguous submit блокируется до ручного разбора.

### Что должен делать оператор

- При manual_intervention_required на старте сначала проверить broker order status и фактические fills по каждому unresolved tid.
- После crash/restart не перезапускать стратегию повторно, если в логах есть ambiguous_submit или unresolved_pending_orders.
- Если повторный submit заблокирован, считать предыдущий submit потенциально ушедшим на биржу, пока брокер не подтверждит обратное.

### Regression-команда Phase 3

c:/Users/Alex_qA/PycharmProjects/trading_manager/.venv/Scripts/python.exe -m pytest tests/test_order_lifecycle.py tests/test_live_engine.py tests/test_autostart.py tests/test_order_executor.py tests/test_reconnect.py -q

## Phase 4 Durable Fill Ledger And Accounting

Дата обновления: 2026-04-06

### Инварианты эксплуатации

- Durable dedup строится на fill_id/exec_key/execution_id и переживает restart без повторной проекции одного и того же fill.
- save_order возвращает explicit status inserted/duplicate/error; append_trade идемпотентен по execution_id.
- FillLedger возвращает ProjectionResult и явно сообщает duplicate, repair и partial projection error.
- History divergence между broker и strategy accounting больше не маскируется blind self-heal: reconcile переводит стратегию в manual intervention path.
- Signal, pending order, fill и обе проекции несут correlation_id для сквозной археологии инцидента.

### Что должен делать оператор

- При projection_divergence или history divergence считать бухгалтерию стратегии недостоверной до reconcile/manual investigation.
- Для проверки replay после рестарта искать один и тот же correlation_id во всех записях order_history, trades_history и pending order snapshots.
- Если duplicate fill replay пришёл после рестарта и проекции не изменились, это штатное поведение durable dedup, а не новый fill.

### Regression-команда Phase 4

c:/Users/Alex_qA/PycharmProjects/trading_manager/.venv/Scripts/python.exe -m pytest tests/test_fill_ledger.py tests/test_storage.py tests/test_order_history.py tests/test_reconciler.py tests/test_trade_recorder.py -q

## Phase 5 Lock Discipline And Collision Guards

Дата обновления: 2026-04-06

### Инварианты эксплуатации

- FinamConnector читает `_connected`, `_positions`, `_accounts` и `_securities` только под `_state_lock` или через snapshot copies.
- Публичные getter'ы коннекторов обязаны отдавать copy-on-read, а не живые mutable структуры shared state.
- submit/close/cancel/reconcile критические пути возвращают explicit outcome/result contract, а не неразличимый bool/None.
- Circuit breaker запрещает только новые risk-increasing submits и при этом явно сигнализирует runtime деградацию оператору.
- На одном `account_id+ticker` разрешён только один владелец стратегии, если не включён `allow_shared_position`.
- Runtime collision guard переводит стратегию в `manual_intervention_required`, если ownership registry видит другой активный owner того же инструмента.

### Что должен делать оператор

- При `manual_intervention_required` с причиной ownership/collision не включать вторую стратегию на тот же инструмент без явного override policy.
- Если close/submit вернул `stale_state` или `transport_error`, считать действие двусмысленным и проверять брокерский факт, а не только локальный лог.
- При circuit breaker открывать расследование по причине деградации, но не ожидать автоматического закрытия позиции.

### Regression-команда Phase 5

c:/Users/Alex_qA/PycharmProjects/trading_manager/.venv/Scripts/python.exe -m pytest tests/test_finam_connector_locks.py tests/test_order_executor.py tests/test_live_engine.py tests/test_autostart.py tests/test_reconciler.py tests/test_position_manager.py tests/test_strategy_flatten.py -q

## Phase 6 Market Data Integrity And Timing Safety

Дата обновления: 2026-04-06

### Инварианты эксплуатации

- Каждый standard signal path несёт market data envelope с `source_ts`, `receive_ts`, `age_ms`, `source_id`, `status`.
- `LiveEngine` не исполняет сигналы на невалидных барах: duplicate timestamp, non-monotonic sequence и broken OHLC переводят стратегию в `manual_intervention_required`.
- `OrderExecutor` отклоняет opening signals при stale quote, crossed bid/ask и non-positive price.
- Signal старше latency budget не исполняется без явного override `allow_stale_signal`.

### Что должен делать оператор

- При `invalid_bars:*` считать market data path недостоверным до проверки источника истории и времени последнего корректного бара.
- При `STALE QUOTE REJECT` не повторять submit вручную, пока не восстановится свежий market data feed.
- Если старый signal нужно исполнить осознанно, это должно быть явное операторское решение с override, а не повторная автоматическая отправка.

### Regression-команда Phase 6

c:/Users/Alex_qA/PycharmProjects/trading_manager/.venv/Scripts/python.exe -m pytest tests/test_live_engine.py tests/test_order_executor.py -q

## Phase 7 Money Strictness And Reservations

Дата обновления: 2026-04-06

### Инварианты эксплуатации

- Любая цена и quantity перед submit нормализуются по instrument constraints через `core/instrument_normalizer.py`.
- Денежные поля в storage пишутся через helpers из `core/money.py`; companion-поля `*_decimal` допустимы для точной миграции.
- Reserve живёт по схеме `reserve -> bind_order -> terminal release`; timeout только помечает его `stale`.
- Ambiguous submit не возвращает капитал автоматически и требует broker/reconcile проверки.

### Что должен делать оператор

- При stale reserve считать buying power потенциально занятым до broker подтверждения.
- При ambiguous submit не отправлять тот же ордер повторно, пока не проверен broker order book.
- Значение PnL `—` в UI трактовать как защитное поведение при недостоверной оценке, а не как нулевой результат.

### Regression-команда Phase 7

c:/Users/Alex_qA/PycharmProjects/trading_manager/.venv/Scripts/python.exe -m pytest tests/test_order_executor.py tests/test_financial_regression.py tests/test_financial_invariants.py -q

## Phase 8 UI Safety And Operator Clarity

Дата обновления: 2026-04-06

### Инварианты эксплуатации

- destructive actions проходят через `DestructiveActionGuard` и временно блокируют повторный клик
- strategy-level и account-level confirmations имеют разные тексты и scope
- runtime status в главной таблице и окне стратегии показывает intent отдельно от actual runtime state
- недостоверный broker-side PnL заменяется на `—`

### Что должен делать оператор

- ориентироваться на runtime/sync статус, а не только на признак active в storage
- использовать strategy-level close для одной стратегии и account-level close только для полного счёта
- временную блокировку destructive action после клика считать штатной защитой от duplicate submit

### Regression-команда Phase 8

c:/Users/Alex_qA/PycharmProjects/trading_manager/.venv/Scripts/python.exe -m pytest tests/test_ui_safety.py tests/test_strategy_flatten.py tests/test_health_server.py -q

## Phase 9 Observability And Alerts

Дата обновления: 2026-04-06

### Инварианты эксплуатации

- `/health`, `/metrics` и `/health/strategies` публикуют runtime snapshot через `core/observability.py`
- `runtime_metrics` хранит drift counters, latency metrics и audit events для submit/reconcile/flatten/repair
- критичные события `history_divergence`, `duplicate_fill_repair`, `stale_data_reject` и `manual_intervention_required` больше не остаются только в локальном логе

### Что должен делать оператор

- использовать health endpoint как первую точку triage при manual intervention и drift-инцидентах
- stale-data alerts трактовать как блокировку новых risk-increasing actions
- перед ручным restart проверять pending orders count и stale state count

### Regression-команда Phase 9

c:/Users/Alex_qA/PycharmProjects/trading_manager/.venv/Scripts/python.exe -m pytest tests/test_health_server.py tests/test_fill_ledger.py tests/test_reconciler.py tests/test_order_executor.py -q

## Phase 10 End-To-End And Invariants

Дата обновления: 2026-04-06

### Инварианты эксплуатации

- startup lifecycle, stop/crash semantics, flatten partials и reconnect fan-out закреплены regression-тестами
- FIFO matching, commission slicing и position book reconstruction проверяются отдельными invariant tests

### Что должен делать оператор

- считать любой regression в lifecycle/flatten/restart блокирующим для релиза

### Regression-команда Phase 10

c:/Users/Alex_qA/PycharmProjects/trading_manager/.venv/Scripts/python.exe -m pytest tests/test_autostart.py tests/test_live_engine.py tests/test_strategy_flatten.py tests/test_order_lifecycle.py tests/test_financial_invariants.py -q

## Phase 11 Migration And Manual Intervention Playbook

Дата обновления: 2026-04-06

### Manual intervention сценарии

- `close_failed`: проверить broker position, затем broker order status, затем pending registry; локальный flat не считать подтверждением
- `stale startup`: проверить unresolved pending orders до повторного включения strategy intent
- `history_divergence`: сверить broker qty, strategy position book и order_history до ручных правок JSON
- `duplicate_fill_repair`: искать один `correlation_id` в order_history, trades_history и audit events

### Migration plan

- strategy records при первом сохранении должны получать `desired_state`, сохраняя legacy `status` для обратной совместимости
- historical orders/trades могут постепенно получать `*_decimal` и `correlation_id` без полной одноразовой миграции
- stale reserves и pending orders после обновления не очищать вручную до первой reconcile-проверки новой версии

### Regression-команда Phase 11

c:/Users/Alex_qA/PycharmProjects/trading_manager/.venv/Scripts/python.exe -m pytest tests/test_health_server.py tests/test_autostart.py tests/test_order_history.py tests/test_storage.py -q
