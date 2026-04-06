# UI ↔ Logic Fix Backlog

Статус: в работе
Дата: 2026-04-06

## P0

- [DONE] P0-1. Добавить в окно настроек UI и сохранение для глобальных risk/execution параметров.
  Ключи: `max_gross_exposure`, `max_account_positions`, `cancel_order_timeout_sec`, `submission_block_ttl_sec`, `signal_latency_budget_sec`, `stale_quote_budget_ms`, `market_data_clock_drift_budget_ms`, `stale_quote_relaxed_phases`.

- [DONE] P0-2. Добавить обязательный UI-блок RiskGuard в окно стратегии и связать его с runtime params.
  Ключи: `max_position_size`, `daily_loss_limit`, `max_trades_per_window`, `trade_window_sec`, `cooldown_after_close_sec`, `circuit_breaker_threshold`, `circuit_breaker_timeout`, `allow_shared_position`.

- [TODO] P0-3. Исправить управление активностью расписания коннекторов.
  Требование: `is_active` должен отображаться в UI, сохраняться без перетирания и восстанавливаться после импорта/перезагрузки.

- [TODO] P0-4. Исправить сохранение вкладки комиссий и dirty-tracking окна настроек.
  Требование: общий Save должен сохранять комиссии, dirty-tracking должен отслеживать `QDoubleSpinBox` и `QComboBox`.

- [TODO] P0-5. Добавить в UI управление `is_enabled` для стратегии.
  Требование: флаг должен быть виден и редактируем в окне стратегии.

## P1

- [TODO] P1-1. Убрать рассинхрон лотности для активной стратегии.
  Требование: либо live-update в engine, либо блокировка редактирования. Предпочтительно live-update.

- [TODO] P1-2. Добавить UI и сохранение для скрытых интеграционных настроек.
  Ключи: `quik_callbacks_port`, `telegram_level`, `webhook_enabled`, `webhook_url`, `webhook_timeout_sec`, `webhook_headers`, `health_server_enabled`, `health_server_port`, `health_server_token`, `paper_mode`.

- [TODO] P1-3. Исправить binding параметров в окне бэктеста.
  Требование: использовать типизированные виджеты для `bool`, `ticker`, `commission`, `select`, `time`, `instruments` где применимо; не ломать режим `auto`.

## P2

- [TODO] P2-1. Снизить config drift и очистить legacy-ключи.
  Ключи: `connector_connect_time`, `connector_disconnect_time`, `connector_days`, а также дубли/миграционный след по `order_mode`.
