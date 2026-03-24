# /new-connector

Добавь новый брокерский коннектор в Trading Strategy Manager.

## Входные данные

Спроси (если не указано):
1. ID коннектора (eng., lowercase): например `"sber"`, `"vtb"`
2. Название для UI: например `"Сбер Инвестиции"`
3. Протокол подключения: REST API / WebSocket / DLL / другое

## Что создать

### 1. `core/<id>_connector.py`

```python
from core.base_connector import BaseConnector
from core.storage import get_setting
from core.moex_api import MOEXClient
from loguru import logger

CONNECTOR_ID = "<id>"

class <Name>Connector(BaseConnector):
    def __init__(self):
        super().__init__()
        self._connected = False
        self.moex_client = MOEXClient()
        # ...

    def connect(self) -> bool: ...
    def disconnect(self): ...
    def is_connected(self) -> bool: ...
    def place_order(self, account_id, ticker, side, quantity,
                    order_type, price, board, agent_name) -> Optional[str]: ...
    def cancel_order(self, order_id, account_id) -> bool: ...
    def get_positions(self, account_id) -> list[dict]: ...
    def get_accounts(self) -> list[dict]: ...
    def get_last_price(self, ticker, board) -> Optional[float]: ...
    def get_order_book(self, board, ticker, depth=10) -> Optional[dict]: ...
    def close_position(self, account_id, ticker, quantity=0, agent_name="") -> bool: ...
    def get_history(self, ticker, board, period, days) -> Optional[pd.DataFrame]: ...
    def get_free_money(self, account_id) -> Optional[float]: ...
    def get_sec_info(self, ticker, board) -> Optional[dict]: ...
    def subscribe_quotes(self, board, ticker): ...
    def unsubscribe_quotes(self, board, ticker): ...
    def get_best_quote(self, board, ticker) -> Optional[dict]: ...

<id>_connector = <Name>Connector()
```

### 2. Зарегистрировать в `core/connector_manager.py`

В функции `register_connectors()` добавить:
```python
from core.<id>_connector import <id>_connector
connector_manager.register("<id>", <id>_connector)
```

### 3. Добавить вкладку в `ui/settings_window.py`

В `_build_ui()` добавить вкладку:
```python
self.tabs.addTab(self._tab_<id>(), "🔌  <Название>")
```

Создать метод `_tab_<id>()` по образцу `_tab_finam()`.

### 4. Добавить расписание в `core/storage.py`

В `_SCHEDULES_DEFAULT` добавить:
```python
"<id>": {
    "connect_time": "06:55", "disconnect_time": "23:45",
    "days": [0, 1, 2, 3, 4], "is_active": True,
}
```

### 5. Добавить в `ui/main_window.py`

В `_setup_core()` добавить callback'и:
```python
<id>_connector._on_connect = lambda: ui_signals.connector_changed.emit("<id>", True)
<id>_connector._on_disconnect = lambda: ui_signals.connector_changed.emit("<id>", False)
<id>_connector._on_error = lambda m: ui_signals.log_message.emit(f"[<Name>] Ошибка: {m}", "error")
```

В `_build_connector_block()` добавить блок для нового коннектора.

## Контракт get_history()

DataFrame должен содержать:
- Колонки: `Open, High, Low, Close, Volume` (с заглавной)
- Индекс: `DatetimeIndex` (datetime объекты)
- Отсортирован по возрастанию

## Контракт get_sec_info()

Возвращаемый dict должен содержать:
```python
{
    "point_cost": float,    # стоимость 1 пункта в рублях
    "minstep": float,       # минимальный шаг цены
    "buy_deposit": float,   # ГО покупателя (фьючерсы)
    "sell_deposit": float,  # ГО продавца (фьючерсы)
    "lotsize": int,         # размер лота
}
```

## Проверка

```bash
python -m py_compile core/<id>_connector.py
python -m py_compile core/connector_manager.py
python -m py_compile ui/settings_window.py
python -m py_compile ui/main_window.py
```
