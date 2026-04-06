# План рефакторинга по результатам review.md

Дата: 2026-04-05

Источник требований: [review.md](review.md)

Цель плана: закрыть все blocker-баги, lifecycle-ошибки, race conditions, расхождения источников истины, UI-risk, проблемы PnL/атрибуции, market data integrity, отказоустойчивости и observability, найденные в аудите.

## Правила исполнения плана

1. Все задачи атомарны: один чёткий результат, минимальный радиус изменения, отдельная проверка.
2. Сначала закрываются P0 safety blockers, потом lifecycle truth, потом durable recovery, потом market/data hardening, затем UI/observability.
3. Нельзя начинать задачи следующих фаз, если открыты незакрытые P0-задачи, создающие прямой риск потери денег.
4. Каждая задача должна завершаться кодом, тестом или явным runtime guard.
5. После каждой фазы нужен regression-прогон и обновление runbook.

## Обозначения

- P0: прямой риск потери денег или нарушение жёсткого требования
- P1: высокий риск рассинхрона, ложного UI, битой бухгалтерии или неправильного lifecycle
- P2: hardening, observability, устойчивость, численная строгость

---

## Фаза 0. Немедленная остановка опасного поведения

- TASK-001 [P0] Удалить auto-close из stop path в [core/live_engine.py](core/live_engine.py). DoD: LiveEngine.stop() никогда не вызывает _emergency_close_position и не читает close_position_on_stop. ВЫПОЛНЕНО.
- TASK-002 [P0] Удалить конфигурационный флаг close_position_on_stop из runtime path и UI/config. DoD: ни один пользовательский сценарий остановки стратегии не содержит скрытого авто-закрытия. ВЫПОЛНЕНО.
- TASK-003 [P0] Удалить auto-close при get_history timeout в [core/live_engine.py](core/live_engine.py). DoD: после серии timeout стратегия переходит в degraded/manual-intervention state без отправки close-ордера. ВЫПОЛНЕНО.
- TASK-004 [P0] Удалить auto-close при circuit breaker в [core/live_engine.py](core/live_engine.py) и связанных прокси-path. DoD: circuit breaker запрещает новые рисковые действия, но не закрывает позицию автоматически. ВЫПОЛНЕНО.
- TASK-005 [P0] Перевести _emergency_close_position из системного lifecycle в явный manual-only workflow. DoD: метод не вызывается из stop, timeout, circuit breaker и crash paths. ВЫПОЛНЕНО.
- TASK-006 [P0] Удалить fallback close_position -> place_order в [core/order_executor.py](core/order_executor.py). DoD: при неуспешном close_position не отправляется встречный market order. ВЫПОЛНЕНО.
- TASK-007 [P0] Ввести явный close_failed / manual_intervention_required result для execution path. DoD: close path при неуспехе помечает стратегию как требующую reconcile/manual action и даёт оператору явный сигнал. ВЫПОЛНЕНО.
- TASK-008 [P0] Исправить контракт запуска LiveEngine: start() должен возвращать bool. DoD: невозможный старт не приводит к регистрации engine в _live_engines. ВЫПОЛНЕНО.
- TASK-009 [P0] Исправить [core/autostart.py](core/autostart.py): регистрировать engine только после подтверждённого running state. DoD: невозможен сценарий "стратегия считается запущенной, но poll loop не существует". ВЫПОЛНЕНО.
- TASK-010 [P0] Добавить regression-тесты на запрет auto-close при stop, timeout и circuit breaker. DoD: есть отдельные тесты, гарантирующие сохранение открытой позиции во всех трёх сценариях. ВЫПОЛНЕНО.

---

## Фаза 1. Корректное владение позицией стратегией

- TASK-011 [P0] Ввести доменную модель strategy-owned position book поверх canonical fills. DoD: у каждой стратегии есть вычислимый остаток открытых лотов независимо от account aggregate. ВЫПОЛНЕНО.
- TASK-012 [P0] Реализовать service для расчёта открытых strategy lots из [core/fill_ledger.py](core/fill_ledger.py). DoD: сервис возвращает open lots, avg entry, side и остатки по partial fills. ВЫПОЛНЕНО.
- TASK-013 [P0] Реализовать strategy-scoped flatten planner. DoD: план закрытия строится от strategy position book, а не от account-level snapshot. ВЫПОЛНЕНО.
- TASK-014 [P0] Реализовать strategy flatten executor с поддержкой partial fills. DoD: при partial fill остаётся корректный остаток, и executor продолжает flatten до broker-confirmed target. ВЫПОЛНЕНО.
- TASK-015 [P0] Разделить UI-действия "Закрыть позиции стратегии" и "Закрыть все позиции счёта". DoD: это разные кнопки, разные подтверждения, разные execution paths. ВЫПОЛНЕНО.
- TASK-016 [P0] Запретить текущий account-level close_all_positions использоваться как strategy close-all. DoD: окно стратегии больше не вызывает generic close_all_positions(account_id) как будто это strategy action. ВЫПОЛНЕНО.
- TASK-017 [P0] Передавать sid стратегии в manual close из [ui/strategy_window.py](ui/strategy_window.py) и связанных панелей. DoD: любое закрытие из окна стратегии несёт strategy identity. ВЫПОЛНЕНО.
- TASK-018 [P0] Исправить атрибуцию manual close fills в [core/finam_connector.py](core/finam_connector.py) и execution layer. DoD: closing fill попадает в историю той стратегии, для которой был инициирован flatten. ВЫПОЛНЕНО.
- TASK-019 [P0] Реализовать корректное завершение strategy order_history после manual strategy flatten. DoD: strategy history после ручного закрытия не содержит ложных open pairs. ВЫПОЛНЕНО.
- TASK-020 [P0] Добавить тесты на strategy flatten, partial fills и отсутствие влияния на чужие позиции на том же счёте. DoD: покрыты сценарии single-strategy, multi-strategy same account и partial flatten. ВЫПОЛНЕНО.

---

## Фаза 2. Lifecycle truth и запуск только после reconcile

- TASK-021 [P0] Реорганизовать startup sequence в [core/autostart.py](core/autostart.py): connect -> snapshot -> reconcile -> enable trading -> on_start. DoD: стратегия не получает торговый runtime до подтверждённой синхронизации. ВЫПОЛНЕНО.
- TASK-022 [P0] Вынести preflight snapshot загрузки позиций, ордеров и баланса в отдельный startup service. DoD: логика startup больше не размазана между autostart и poll loop. ВЫПОЛНЕНО.
- TASK-023 [P1] Перенести initial detect_position/reconcile из poll loop в явную фазу инициализации. DoD: engine стартует в synced или explicit degraded state, но не в ambiguous unknown-trading state. ВЫПОЛНЕНО.
- TASK-024 [P1] Разделить user intent status и runtime status стратегии. DoD: storage хранит desired_state, runtime хранит actual_state, и UI показывает оба без путаницы. ВЫПОЛНЕНО.
- TASK-025 [P1] Ввести формальную state machine стратегии: initializing, synced, stale, trading, degraded, manual_intervention_required, stopping, stopped, failed_start. DoD: переходы задокументированы и покрыты тестами. ВЫПОЛНЕНО.
- TASK-026 [P1] Исправить watchdog в [core/autostart.py](core/autostart.py): он должен менять runtime state, а не подменять user intent. DoD: disconnect больше не создаёт ложный active UI state. ВЫПОЛНЕНО.
- TASK-027 [P1] Добавить UI-индикацию runtime state и sync status отдельно от business status. DoD: пользователь видит difference между "стратегия включена" и "движок реально торгует/не торгует". ВЫПОЛНЕНО.
- TASK-028 [P1] Добавить тесты на failed_start, stale startup, reconnect startup и watchdog stop/start semantics. DoD: runtime status и desired status не расходятся незаметно. ВЫПОЛНЕНО.

---

## Фаза 3. Reconnect, pending orders и восстановление после рестарта

- TASK-029 [P1] Заменить on_reconnect setter на subscribe_reconnect в [core/live_engine.py](core/live_engine.py). DoD: несколько стратегий одновременно получают reconnect callbacks. ВЫПОЛНЕНО.
- TASK-030 [P1] Добавить unsubscribe_reconnect при остановке engine. DoD: stop/restart не накапливает висящие callbacks. ВЫПОЛНЕНО.
- TASK-031 [P1] Ввести durable pending order registry в storage. DoD: pending order state сохраняется на диск и переживает crash/restart. ВЫПОЛНЕНО.
- TASK-032 [P1] Сохранять pending orders при submit, update и terminal transitions. DoD: для каждого in-flight order есть durable запись с terminal outcome. ВЫПОЛНЕНО.
- TASK-033 [P1] Реализовать startup recovery pending orders из durable registry + broker order book. DoD: после рестарта система восстанавливает in-flight orders и не теряет partial fills. ВЫПОЛНЕНО.
- TASK-034 [P1] Реализовать unresolved-order recovery policy. DoD: если broker и registry расходятся, стратегия переводится в manual_intervention_required, а не в blind self-heal. ВЫПОЛНЕНО.
- TASK-035 [P1] Ввести idempotency key model для order submission. DoD: повторный submit одного и того же действия не порождает duplicate live order при неясном ответе брокера. ВЫПОЛНЕНО.
- TASK-036 [P1] Добавить restart/regression тесты для: order sent/no ack, late fill after restart, duplicate resend protection. DoD: покрыты ключевые сценарии рестарта, описанные в review. ВЫПОЛНЕНО.

---

## Фаза 4. Durable fill ledger и консистентная бухгалтерия

- TASK-037 [P1] Сделать fill dedup durable по fill_id, а не только in-memory. DoD: повторный fill после рестарта не дублирует canonical запись. ВЫПОЛНЕНО.
- TASK-038 [P1] Сделать [core/storage.py](core/storage.py) append_trade идемпотентным по execution_id. DoD: trades_history не получает дублей при повторной проекции одного fill. ВЫПОЛНЕНО.
- TASK-039 [P1] Сделать save_order idempotent contract явным: вернуть статус inserted/duplicate/error. DoD: вызывающий код знает, была ли проекция реально записана. ВЫПОЛНЕНО.
- TASK-040 [P1] Вынести проекцию fill -> order_history/trades_history в единый atomic projection result. DoD: нельзя получить ситуацию "одна проекция записана, другая нет" без explicit error state. ВЫПОЛНЕНО.
- TASK-041 [P1] Добавить reconcile repair path для history divergence. DoD: reconcile умеет чинить не только runtime position, но и strategy accounting divergence либо переводить её в manual intervention. ВЫПОЛНЕНО.
- TASK-042 [P1] Ввести audit trail для signal -> order -> fill -> projection с correlation id. DoD: по одному идентификатору можно восстановить полный путь исполнения. ВЫПОЛНЕНО.
- TASK-043 [P1] Добавить тесты на durable dedup, projection idempotency и repair history divergence. DoD: сценарии duplicate callback, restart replay и partial projection покрыты тестами. ВЫПОЛНЕНО.

---

## Фаза 5. Риск, брокерские гонки и lock discipline

- TASK-044 [P1] Провести полный lock hardening в [core/finam_connector.py](core/finam_connector.py) для _connected, _positions, _accounts, _securities. DoD: все shared reads/writes идут через _state_lock или snapshot copies. ВЫПОЛНЕНО.
- TASK-045 [P1] Проверить и исправить copy-on-read contract у connector getters. DoD: наружу не выдаются живые mutable структуры shared state. ВЫПОЛНЕНО.
- TASK-046 [P1] Добавить thread-safe contract tests для FinamConnector shared state. DoD: есть тесты на concurrent callback write + public getter read. ВЫПОЛНЕНО.
- TASK-047 [P1] Перевести close/submit/reconcile critical paths на единый outcome enum вместо bool/None ambiguity. DoD: execution layer различает not_found, rejected, transport_error, stale_state и success. ВЫПОЛНЕНО.
- TASK-048 [P1] Усилить circuit breaker: он должен переводить только в no-new-risk state. DoD: breaker не вызывает destructive side effects и явно сигнализирует оператору. ВЫПОЛНЕНО.
- TASK-049 [P1] Добавить account/ticker ownership validator перед стартом стратегии. DoD: запуск второй стратегии на тот же account+ticker блокируется либо требует explicit override policy. ВЫПОЛНЕНО.
- TASK-050 [P1] Добавить runtime guard против multi-strategy collision на одном инструменте. DoD: система не допускает молчаливого совместного владения aggregate broker position без специального режима. ВЫПОЛНЕНО.

---

## Фаза 6. Market data integrity и timing safety

- TASK-051 [P1] Ввести market data envelope: source_ts, receive_ts, age_ms, source_id, status. DoD: каждый signal path знает freshness данных. ВЫПОЛНЕНО.
- TASK-052 [P1] Добавить валидацию monotonicity и continuity для history bars. DoD: strategy signal не строится на неотсортированных, дублированных или очевидно битых барах. ВЫПОЛНЕНО.
- TASK-053 [P1] Добавить validation на stuck price / empty quote / invalid bid-ask. DoD: execution path не использует очевидно невалидную рыночную цену. ВЫПОЛНЕНО.
- TASK-054 [P1] Добавить stale quote guard в execution path. DoD: ордер не отправляется по quote/last price старше заданного age budget. ВЫПОЛНЕНО.
- TASK-055 [P1] Добавить latency budget между signal timestamp и order send timestamp. DoD: слишком старый signal не исполняется без явного разрешения. ВЫПОЛНЕНО.
- TASK-056 [P1] Добавить тесты на stale data reject, duplicate bars, out-of-order bars и broken quote payload. DoD: market data integrity покрыта regression-тестами. ВЫПОЛНЕНО.

---

## Фаза 7. Денежная строгость, комиссии, баланс и численная точность

- TASK-057 [P1] Нормализовать все цены перед submit и valuation по minstep/tick size. DoD: система не отправляет и не считает значения вне допустимой биржевой сетки. ВЫПОЛНЕНО.
- TASK-058 [P1] Ввести единый normalizer qty/price/notional по instrument constraints. DoD: validation едина для strategies, execution и manual UI. ВЫПОЛНЕНО.
- TASK-059 [P1] Перевести денежные persistence-границы на Decimal или integer minor-units. DoD: комиссии, pnl и balance snapshots не накапливают float drift в storage. ВЫПОЛНЕНО.
- TASK-060 [P1] Переписать reservation arithmetic с явной связью reserve -> order_id -> terminal release. DoD: резерв не живёт отдельной жизнью от pending order lifecycle. ВЫПОЛНЕНО.
- TASK-061 [P1] Убрать silent stale reservation eviction как основной recovery mechanism. DoD: eviction либо подтверждён reconcile, либо переводит order в manual investigation. ВЫПОЛНЕНО.
- TASK-062 [P1] Добавить инвариантные тесты: position = sum(fills), realized pnl = sum(closed pairs), reserved capital согласован с pending orders. DoD: базовые финансовые инварианты автоматически проверяются. ВЫПОЛНЕНО.

---

## Фаза 8. UI safety и human-factor hardening

- TASK-063 [P1] Перевести destructive UI actions в pending/disabled state на время исполнения. DoD: повторный клик по close/flatten невозможен до завершения операции. ВЫПОЛНЕНО.
- TASK-064 [P1] Изменить тексты подтверждений в UI: strategy-level и account-level actions должны быть недвусмысленны. DoD: подтверждение явно говорит, что именно будет закрыто. ВЫПОЛНЕНО.
- TASK-065 [P1] Убрать ложный broker-side pnl из strategy UI там, где нет достоверной net valuation. DoD: UI либо показывает валидный net PnL, либо помечает значение как unavailable. ВЫПОЛНЕНО.
- TASK-066 [P1] Использовать [core/valuation_service.py](core/valuation_service.py) для offline/runtime position display whenever possible. DoD: одна денежная формула используется и в engine UI, и в fallback UI. ВЫПОЛНЕНО.
- TASK-067 [P1] Показывать sync_status, runtime_state и manual_intervention_required прямо в [ui/main_window.py](ui/main_window.py) и [ui/strategy_window.py](ui/strategy_window.py). DoD: пользователь не может спутать active intent с real trading readiness. ВЫПОЛНЕНО.
- TASK-068 [P1] Добавить UI regression-тесты на кнопки close, pending state, status rendering и предупреждения destructive actions. DoD: human-factor критичные места покрыты тестами. ВЫПОЛНЕНО.

---

## Фаза 9. Observability, аудит и эксплуатационные метрики

- TASK-069 [P2] Ввести метрики по drift: broker_vs_engine_qty, broker_vs_history_qty, pending_orders_count, stale_state_count. DoD: метрики доступны для runtime наблюдения. ВЫПОЛНЕНО.
- TASK-070 [P2] Ввести метрики latency: signal_to_submit_ms, submit_to_ack_ms, ack_to_fill_ms, reconnect_to_resync_ms. DoD: можно видеть деградацию до инцидента, а не после. ВЫПОЛНЕНО.
- TASK-071 [P2] Ввести structured audit events для startup, reconcile, flatten, close_failed, duplicate_fill_repair. DoD: из логов можно восстановить любой money event без ручной археологии. ВЫПОЛНЕНО.
- TASK-072 [P2] Добавить alerting rules для manual_intervention_required, stale_data_reject, history_divergence, duplicate_fill_repair. DoD: критичные события не остаются только в локальном логе. ВЫПОЛНЕНО.
- TASK-073 [P2] Добавить health-check панель или endpoint с runtime state и критичными счетчиками. DoD: оператор видит текущую торговую готовность без чтения логов. ВЫПОЛНЕНО.

---

## Фаза 10. Тестовый контур и формальные инварианты

- TASK-074 [P1] Добавить end-to-end тест startup lifecycle: connect -> snapshot -> reconcile -> start trading. DoD: неправильный порядок больше не регрессирует. ВЫПОЛНЕНО.
- TASK-075 [P1] Добавить end-to-end тест stop/crash semantics: позиция сохраняется открытой, стратегия переходит в safe runtime state. DoD: запрет auto-close закреплён тестом. ВЫПОЛНЕНО.
- TASK-076 [P1] Добавить end-to-end тест strategy flatten с partial fills и delayed broker confirmations. DoD: flatten корректно завершается или уходит в manual intervention. ВЫПОЛНЕНО.
- TASK-077 [P1] Добавить reconnect multi-engine test. DoD: все активные стратегии получают resync callback после reconnect. ВЫПОЛНЕНО.
- TASK-078 [P1] Добавить restart recovery test для pending orders и durable fill dedup. DoD: после рестарта нет потери partial fills и нет дубликатов. ВЫПОЛНЕНО.
- TASK-079 [P2] Добавить property/invariant tests для FIFO matching, commission slicing и position book reconstruction. DoD: инварианты проверяются на случайных последовательностях fills. ВЫПОЛНЕНО.
- TASK-080 [P2] Добавить test matrix для multi-strategy same account / same ticker / same connector. DoD: collision policy закреплена тестами. ВЫПОЛНЕНО.

---

## Фаза 11. Документация и эксплуатационный контур

- TASK-081 [P2] Обновить [docs/decisions.md](docs/decisions.md) и профильные REFERENCE-файлы новой lifecycle-моделью. DoD: документация совпадает с кодом. ВЫПОЛНЕНО.
- TASK-082 [P2] Добавить runbook для manual intervention: unresolved close, stale startup, history divergence, restart recovery. DoD: оператор знает, что делать без чтения кода. ВЫПОЛНЕНО.
- TASK-083 [P2] Добавить описание инвариантов и ownership policy стратегии в developer docs. DoD: новые изменения не ломают strategy-owned position model по незнанию. ВЫПОЛНЕНО.
- TASK-084 [P2] Добавить migration plan для существующих strategy records и historical orders после перехода на новую модель flatten/ownership. DoD: обновление не ломает существующие данные молча. ВЫПОЛНЕНО.

---

## Рекомендуемый порядок исполнения

1. TASK-001 .. TASK-010
2. TASK-011 .. TASK-020
3. TASK-021 .. TASK-036
4. TASK-037 .. TASK-050
5. TASK-051 .. TASK-062
6. TASK-063 .. TASK-073
7. TASK-074 .. TASK-084

## Критерий завершения всей программы работ

План считается выполненным только когда одновременно выполнены все условия:

1. В коде отсутствует любой скрытый auto-close path.
2. Strategy flatten реализован как отдельный безопасный workflow.
3. Startup происходит только после snapshot и reconcile.
4. После restart сохраняются pending orders и не дублируются fills.
5. UI не вводит пользователя в заблуждение по статусу, pnl и масштабу destructive actions.
6. Multi-strategy collision policy формализована и enforced.
7. Все ключевые инварианты закреплены тестами.

