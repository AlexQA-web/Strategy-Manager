# ui/ — контекст для Claude Code

Общие правила проекта: см. корневой `CLAUDE.md` и `@../.claude/rules.md`.

## Главное правило

Все обновления виджетов — **только из главного потока Qt**.
Из фоновых потоков — только через `ui_signals.*` или `QTimer.singleShot(0, callback)`.

```python
# ✅ Правильно из фонового потока:
from ui.main_window import ui_signals
ui_signals.strategies_changed.emit()
QTimer.singleShot(0, lambda: self.label.setText("готово"))

# ❌ Запрещено из фонового потока:
self.label.setText("готово")   # UB, крэш
```

## Цветовая тема — Catppuccin Mocha

```python
# Основные цвета (не менять):
BG_MAIN    = "#1e1e2e"   # фон окна
BG_SURFACE = "#181825"   # фон таблиц, инпутов
OVERLAY    = "#313244"   # кнопки, карточки
BORDER     = "#45475a"   # границы
TEXT       = "#cdd6f4"   # основной текст
SUBTEXT    = "#a6adc8"   # вторичный текст
MUTED      = "#6c7086"   # подсказки, заглушки

BLUE       = "#89b4fa"   # акцент, ссылки, заголовки
GREEN      = "#a6e3a1"   # прибыль, успех, лонг, подключён
RED        = "#f38ba8"   # убыток, ошибка, шорт, отключён
YELLOW     = "#f9e2af"   # предупреждение, комиссия
TEAL       = "#94e2d5"   # нейтральные метрики
ORANGE     = "#fab387"   # дочерние строки, предупреждения
```

## ui_signals — межпоточные сигналы

```python
from ui.main_window import ui_signals

ui_signals.log_message.emit("текст", "info")      # уровни: debug/info/warning/error
ui_signals.connector_changed.emit("finam", True)   # обновить статус коннектора
ui_signals.strategies_changed.emit()               # перерисовать таблицу агентов
ui_signals.positions_updated.emit()               # обновить панель позиций
```

## param_widgets.py — добавление нового типа

```python
# 1. Создать класс:
class MyParamWidget(BaseParamWidget):
    def __init__(self, key, meta, current_value, connector_id=None, parent=None):
        super().__init__(key, meta, current_value, connector_id, parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.widget = QSomeWidget()
        self.widget.setFixedWidth(120)
        lay.addWidget(self.widget)
        val = current_value if current_value is not None else meta.get("default")
        if val is not None:
            self.set_value(val)

    def get_value(self): return ...
    def set_value(self, value): ...
    def validate(self) -> tuple[bool, str]: return True, ""

# 2. Зарегистрировать в конце param_widgets.py:
ParamWidgetFactory.register("my_type", MyParamWidget)
```

**Команда:** `/new-param-type`

## ScrollArea — обязательный NoScroll

Любой SpinBox или ComboBox внутри QScrollArea должен игнорировать wheel без фокуса:

```python
class _NoScrollSpinBox(QSpinBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    def wheelEvent(self, event):
        if not self.hasFocus():
            event.ignore()
            return
        super().wheelEvent(event)
# Аналогично для QDoubleSpinBox и QComboBox
```

Готовые реализации: `commission_settings_widget.py` → `_NoScrollSpinBox`, `_NoScrollComboBox`.

## settings_window.py — паттерн _SettingsMixin

Настройки используют mixin чтобы работать и как QDialog, и как QWidget:
- `SettingsWindow(QDialog, _SettingsMixin)` — модальный диалог
- `SettingsWidget(QWidget, _SettingsMixin)` — вкладка в главном окне

Новая вкладка настроек:
1. Метод `_tab_<name>(self) -> QWidget` в `_SettingsMixin`
2. `self.tabs.addTab(self._tab_<name>(), "🔌  Название")` в `_build_ui()`
3. Сохранение в `_save_all()`

## chart_window.py — DataLoader

Тяжёлые запросы к коннектору вынесены в `DataLoader(QThread)`:
- `data_ready` сигнал → `_on_data_ready()` в GUI-потоке
- `_cancelled` флаг — устанавливать в `closeEvent`
- `precalc_fn` — pandas-тяжёлые вычисления тоже в потоке

```python
loader = DataLoader(ticker, board, days, interval, connector_id, precalc_fn)
loader.data_ready.connect(self._on_data_ready)
loader.error.connect(self._on_error)
loader.start()
```

## strategy_window.py — ParamWidgetFactory

Вкладка «Параметры» строится автоматически из `get_params()` стратегии:

```python
from ui.param_widgets import ParamWidgetFactory

widget = ParamWidgetFactory.create(key, meta, current_value, connector_id)
form.addRow(f"{label}:", widget)
self._param_widgets[key] = widget
```

Сохранение: `widget.get_value()` для всех `BaseParamWidget`-наследников.

Специальный случай `ticker` → сохранять board отдельно:
```python
if isinstance(widget, TickerParamWidget):
    params[key] = widget.get_value()    # тикер
    params["board"] = widget.get_board() # борд
```

## ticker_selector.py — API

```python
from ui.ticker_selector import TickerSelector

sel = TickerSelector(connector_id="finam", current_ticker="SiM6", current_board="FUT")
sel.ticker_changed.connect(lambda t: ...)
sel.board_changed.connect(lambda b: ...)

ticker = sel.ticker()   # str
board  = sel.board()    # str
sel.set_ticker_and_board("SiM6", "FUT")   # без эмиссии сигналов
sel.set_connector("quik")                 # переключить коннектор
```

## main_window.py — таблица агентов

Таблица перестраивается полностью в `_refresh_table()` каждые 5 сек.
Порядок строк хранится в `self._row_order: list[str]` (список sid).

`AgentCellWidget` — виджет ячейки «Агент» с кнопками ▶ ■ 📊.
`TickerExpandWidget` — кнопка +/- для разворачивания корзины инструментов.
`AgentTable(QTableWidget)` — drag-and-drop строк через mouse events (не InternalMove).

**Не использовать** `QTableWidget.setDragDropMode(InternalMove)` — ломает setCellWidget.

## Иконки и обозначения

```
▶  — запуск стратегии     (зелёный #a6e3a1)
■  — стоп стратегии       (красный #f38ba8)
📊 — бэктест
📈 — открыть график
🟢 — подключён / активен
🔴 — отключён / остановлен
🟡 — ожидание
🔥 — ошибка
└  — дочерняя строка (инструмент из корзины)
```

## Диалоги — минимум кода

Использовать существующие диалоги вместо новых:
- Выбор файла: `QFileDialog.getOpenFileName()`
- Подтверждение: `QMessageBox.question()`
- Ошибка: `QMessageBox.critical()`
- Информация: `QMessageBox.information()`
