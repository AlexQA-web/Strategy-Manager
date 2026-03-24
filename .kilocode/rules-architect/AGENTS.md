# Project Architecture Rules (Non-Obvious Only)

- Критичная асимметрия: `core/finam_connector.py` (DLL callback-модель) и `core/quik_connector.py` (polling-модель) живут в одном runtime, поэтому блокирующие вызовы в GUI особенно опасны.
- Главный runtime-поток торговли: `LiveEngine` работает как polling-loop по таймфрейму и обязан переживать зависания источника истории через timeout + skip tick (вместо блокировки цикла).
- В `LiveEngine` состояние позиции и флаг ордера проектно связаны одним lock (`_position_lock`); разделение этой атомарности ломает гарантию single-entry.
- Подсистема chase (`core/chase_order.py`) архитектурно зависит от delayed terminal status: отписка watcher до финального статуса приводит к потере partial fill-событий.
- Lock-ordering является частью архитектуры: `_chase_lock` и `_position_lock` нельзя держать вложенно (иначе взаимоблокировка при stop/cleanup).
- Слой хранилища (`core/storage.py`) — не просто helper: это обязательный serialization boundary для `data/*.json` с `_write_lock`, `.tmp`-replace и `.bak` recovery.
- `point_cost` встроен как cross-cutting инвариант расчётов: приоритет MOEX API над DLL должен сохраняться во всех путях (коннектор, live, комиссии).
- Инициализация приложения имеет фиксированный порядок: создание UI (`MainWindow`) предшествует регистрации/автоподключению коннекторов; import-time wiring считается архитектурной ошибкой.
