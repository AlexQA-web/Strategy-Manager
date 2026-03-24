# Паттерны кода — Trading Strategy Manager

Справочник для Claude Code: устоявшиеся решения для типичных задач проекта.
Использовать эти паттерны вместо изобретения новых вариантов.

---

## 1. Фоновый поток с результатом в GUI

```python
import threading
from PyQt6.QtCore import QTimer

def _fetch_data_background(self):
    """Запускает тяжёлый вызов в daemon-потоке, результат через QTimer.singleShot."""
    if getattr(self, "_fetching", False):
        return  # уже в процессе
    self._fetching = True

    def _worker():
        try:
            result = self._connector.get_something()  # блокирующий вызов
        except Exception as e:
            logger.warning(f"[ClassName] fetch error: {e}")
            result = None
        finally:
            self._fetching = False
        # Передаём результат в GUI-поток
        QTimer.singleShot(0, lambda: self._on_data_ready(result))

    threading.Thread(target=_worker, daemon=True).start()

def _on_data_ready(self, result):
    """Вызывается в GUI-потоке."""
    if result is None:
        return
    # обновляем виджеты...
```

---

## 2. Фоновый поток с таймаутом (для get_history)

```python
def _load_with_timeout(self, timeout: float = 30.0):
    """Загружает данные с таймаутом. Не блокирует poll_loop."""
    result = {"df": None, "error": None}

    def _fetch():
        try:
            result["df"] = self._connector.get_history(
                ticker=self._ticker, board=self._board,
                period=self._period_str, days=self._days,
            )
        except Exception as e:
            result["error"] = e

    t = threading.Thread(target=_fetch, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        logger.warning(f"[LiveEngine] get_history завис (>{timeout}с), пропускаем тик")
        return None

    if result["error"]:
        logger.error(f"[LiveEngine] get_history error: {result['error']}")
        return None

    return result["df"]
```

---

## 3. Атомарная проверка позиции + установка флага

```python
# В _execute_signal() — единственный правильный способ
def _execute_signal(self, signal: dict):
    action = signal.get("action")

    if action in ("buy", "sell"):
        with self._position_lock:
            if self._position != 0:
                logger.warning(f"[{self._strategy_id}] Позиция уже открыта, игнорируем {action}")
                return
            if self._order_mode == "limit":
                if self._order_in_flight:
                    logger.warning(f"[{self._strategy_id}] Ордер уже в работе")
                    return
                self._order_in_flight = True  # внутри того же lock!
        # Дальше исполняем...
```

---

## 4. Мониторинг ордера в фоновом потоке

```python
def _monitor_order(self, tid: str, side: str, qty: int, price: float,
                   comment: str, is_close: bool) -> None:
    """Шаблон мониторинга — запускать через threading.Thread(daemon=True)."""
    _TERMINAL = {"matched", "cancelled", "canceled", "denied", "removed", "expired", "killed"}
    TIMEOUT_SEC = 30

    filled = 0
    deadline = time.monotonic() + TIMEOUT_SEC

    while self._running and time.monotonic() < deadline:
        try:
            info = self._connector.get_order_status(tid)
        except Exception as e:
            logger.warning(f"[{self._strategy_id}] get_order_status {tid}: {e}")
            info = None

        if info:
            status = info.get("status", "")
            balance = info.get("balance")
            quantity = info.get("quantity")
            if balance is not None and quantity is not None:
                filled = int(quantity) - int(balance)
            if status in _TERMINAL:
                break

        time.sleep(0.5)

    # Обновляем позицию под lock
    with self._position_lock:
        if filled > 0:
            if is_close:
                self._position = 0
                self._position_qty = 0
                self._entry_price = 0.0
            else:
                self._position = 1 if side == "buy" else -1
                self._position_qty = filled if side == "buy" else -filled
                self._entry_price = price
            self._record_trade(side, filled, price, comment, order_type="market")
```

---

## 5. Новый тип параметра (BaseParamWidget)

```python
from ui.param_widgets import BaseParamWidget, ParamWidgetFactory
from PyQt6.QtWidgets import QHBoxLayout, QSpinBox
from typing import Any, Tuple

class MyParamWidget(BaseParamWidget):
    def __init__(self, key: str, meta: dict, current_value: Any,
                 connector_id: str = None, parent=None):
        super().__init__(key, meta, current_value, connector_id, parent)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.widget = QSpinBox()
        self.widget.setFixedWidth(120)
        self.widget.setRange(meta.get("min", 0), meta.get("max", 9999))
        lay.addWidget(self.widget)

        val = current_value if current_value is not None else meta.get("default", 0)
        self.set_value(val)

        if self.toolTip():
            self.widget.setToolTip(self.toolTip())

    def get_value(self) -> int:
        return self.widget.value()

    def set_value(self, value: Any):
        try:
            self.widget.setValue(int(value))
        except (ValueError, TypeError):
            pass

    def validate(self) -> Tuple[bool, str]:
        v = self.get_value()
        min_v = self.meta.get("min")
        max_v = self.meta.get("max")
        if min_v is not None and v < min_v:
            return False, f"Значение должно быть >= {min_v}"
        if max_v is not None and v > max_v:
            return False, f"Значение должно быть <= {max_v}"
        return True, ""

# Регистрация в конце файла param_widgets.py:
ParamWidgetFactory.register("my_type", MyParamWidget)
```

---

## 6. SpinBox без случайного scroll

```python
from PyQt6.QtWidgets import QSpinBox, QDoubleSpinBox, QComboBox
from PyQt6.QtCore import Qt

class _NoScrollSpinBox(QSpinBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):
        if not self.hasFocus():
            event.ignore()
            return
        super().wheelEvent(event)

# Аналогично для QDoubleSpinBox и QComboBox.
# Использовать ВЕЗДЕ где виджет находится внутри QScrollArea.
```

---

## 7. Атомарная запись одной настройки

```python
# Правильно — использует _write_lock и читает с диска минуя кэш:
from core.storage import save_setting
save_setting("my_key", "my_value")

# Неправильно — race condition при конкурентных вызовах:
settings = get_settings()
settings["my_key"] = "my_value"
save_settings(settings)  # ← другой поток может затереть
```

---

## 8. Read-modify-write нескольких ключей атомарно

```python
# В storage.py для операций требующих атомарности нескольких ключей:
from core.storage import _write_lock, _read, _write_unsafe, SETTINGS_FILE

with _write_lock:
    settings = _read(SETTINGS_FILE, use_cache=False)  # use_cache=False обязательно!
    settings["key1"] = value1
    settings["key2"] = value2
    _write_unsafe(SETTINGS_FILE, settings)  # без lock (он уже захвачен)
```

---

## 9. Синглтон-модуль

```python
# Паттерн для core-модулей с глобальным экземпляром:

class MyManager:
    def __init__(self):
        self._state = {}
        self._lock = threading.Lock()

    def do_something(self):
        with self._lock:
            ...

# В конце файла — единственный экземпляр:
my_manager = MyManager()

# В других модулях:
from core.my_manager import my_manager
```

---

## 10. Межпоточная передача данных в GUI

```python
# Правильно — через сигналы QObject:
from ui.main_window import ui_signals
ui_signals.strategies_changed.emit()       # обновить таблицу
ui_signals.log_message.emit("текст", "info")  # добавить в лог

# Правильно — QTimer.singleShot из daemon-потока:
from PyQt6.QtCore import QTimer
QTimer.singleShot(0, lambda: self._update_label(result))

# Неправильно — прямой вызов методов виджета из потока:
self.lbl_status.setText("новый текст")  # ← UB, крэш
```

---

## 11. Проверка NaN в барах

```python
def _is_valid(val) -> bool:
    """Проверяет что значение не None и не NaN."""
    if val is None:
        return False
    try:
        return val == val  # NaN != NaN
    except Exception:
        return False

# Использование:
sma = current.get("_sma")
if not _is_valid(sma):
    return {"action": None}
```

---

## 12. Паттерн подписки на котировки (refcount)

```python
# В коннекторе — refcount чтобы не отписаться раньше времени:
def subscribe_quotes(self, board: str, seccode: str):
    key = (board, seccode)
    with self._quotes_lock:
        cnt = self._quote_subscribers.get(key, 0)
        self._quote_subscribers[key] = cnt + 1
        if cnt > 0:
            return  # уже подписаны
    # реальная подписка...

def unsubscribe_quotes(self, board: str, seccode: str):
    key = (board, seccode)
    with self._quotes_lock:
        cnt = self._quote_subscribers.get(key, 0)
        if cnt <= 1:
            self._quote_subscribers.pop(key, None)
        else:
            self._quote_subscribers[key] = cnt - 1
            return  # ещё есть подписчики
    # реальная отписка...
```

---

## 13. Запись сделки в order_history

```python
# Всегда через make_order() + save_order():
from core.order_history import make_order, save_order

commission_rub = commission_manager.calculate(
    ticker=ticker, board=board, quantity=qty,
    price=price, order_role=order_role,
    point_cost=point_cost, connector_id=connector_id,
)
commission_per_lot = commission_rub / qty if qty > 0 else commission_rub

order = make_order(
    strategy_id=strategy_id,
    ticker=ticker,
    side=side,           # "buy" | "sell"
    quantity=qty,
    price=price,
    board=board,
    comment=comment,
    commission=commission_per_lot,  # руб/лот, ОДНА сторона
    point_cost=point_cost,
)
save_order(order)
```

---

## 14. Паттерн on_precalc с групповой агрегацией

```python
# Для расчёта дневных уровней (дорого если делать циклом):
def on_precalc(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    # Дневные хай/лоу
    daily = df.groupby("date_int").agg(
        session_high=("high", "max"),
        session_low=("low", "min"),
    )
    # Значения предыдущей сессии
    daily["_prev_high"] = daily["session_high"].shift(1)
    daily["_prev_low"]  = daily["session_low"].shift(1)
    daily["_range"]     = daily["_prev_high"] - daily["_prev_low"]

    # Мержим обратно — каждый бар получает значения своей даты
    df = df.merge(
        daily[["_prev_high", "_prev_low", "_range"]],
        left_on="date_int",
        right_index=True,
        how="left",
    )
    return df
```
