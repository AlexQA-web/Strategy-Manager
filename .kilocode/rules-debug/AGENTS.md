# Project Debug Rules (Non-Obvious Only)

- Симптом «двойной вход в позицию»: проверить, что в `LiveEngine` проверки `_position`/`_order_in_flight` и установка `_order_in_flight=True` не разнесены по разным lock-секциям.
- Симптом «GUI подвисает при графике/лотности»: искать прямые вызовы `connector.get_history()`/`connector.get_sec_info()` из GUI-потока; в проекте это всегда выносится в фон + возврат в GUI через `QTimer.singleShot`/`ui_signals`.
- Симптом «теряются partial fills при cancel chase»: в `core/chase_order.py` порядок строго `cancel_order()` → `_wait_for_terminal_status()` → `unwatch_order()`.
- Симптом «stop() зависает»: проверить отсутствие вложенности `_chase_lock` и `_position_lock`; в обработчиках завершения chase lock-порядок критичен.
- Симптом «PnL по фьючерсу неверный»: в `finam_connector.get_sec_info()` должен сохраняться приоритет `point_cost` из MOEX API, а не из DLL.
- Симптом «дубли сделок»: `core/finam_connector.py` должен дедуплицировать по `tradeno` под `_processed_trades_lock`.
- Симптом «потеря данных JSON при конкуренции»: любые read-modify-write в `data/*.json` должны идти через `core/storage.py` под `_write_lock` и с `use_cache=False`.
- Симптом «повторный connect Финам намертво висит»: убедиться, что `Initialize()` не вызывается повторно после установки `self._initialized=True`.
