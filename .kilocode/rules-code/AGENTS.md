# Project Coding Rules (Non-Obvious Only)

- Позиция/ордер в `core/live_engine.py`: проверка `_position` и `_order_in_flight` + установка `_order_in_flight=True` выполняются в одном `self._position_lock`.
- Для JSON из `data/` использовать только `core/storage.py`; одиночный update — `save_setting()`, multi-key update — `_write_lock` + `_read(..., use_cache=False)` + `_write_unsafe(...)`.
- Из фонового потока UI обновлять только через `ui.main_window.ui_signals` или `QTimer.singleShot(0, ...)`; прямые вызовы виджетов из worker-потоков запрещены.
- Для отмены chase в `core/chase_order.py`: сначала `_wait_for_terminal_status(...)`, только потом `unwatch_order(...)` — иначе теряются partial fills.
- Никогда не вкладывать `_chase_lock` и `_position_lock` (см. `LiveEngine.stop()` и обработчики завершения chase).
- В `on_precalc()` стратегий — только векторный pandas; циклы по барам (`for i in range(len(df))`) считаются ошибкой производительности.
- `point_cost` для фьючерсов: приоритет значения из MOEX API, данные TransAQ DLL — только fallback.
- `FinamConnector.Initialize()` должен вызываться ровно один раз за lifecycle процесса (`self._initialized`).
- Регистрация коннекторов происходит после создания UI (путь инициализации: `main.py` → `MainWindow()` → register_connectors), не на import-time.
