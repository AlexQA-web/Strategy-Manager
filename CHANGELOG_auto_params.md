# Changelog: Автоматическая генерация UI для параметров стратегий

## Дата: 2026-03-22

## Описание изменений

Реализована система автоматической генерации UI для параметров стратегий. Теперь при добавлении новой стратегии не нужно вручную прописывать код для создания полей настроек — они генерируются автоматически на основе метаданных из функции `get_params()`.

---

## Новые файлы

### 1. [`ui/param_widgets.py`](ui/param_widgets.py:1) — Фабрика виджетов параметров

**Компоненты:**

- **[`BaseParamWidget`](ui/param_widgets.py:13)** — базовый абстрактный класс для всех виджетов параметров
  - `get_value()` — получение текущего значения
  - `set_value(value)` — установка значения
  - `validate()` — валидация, возвращает (bool, str)

- **Виджеты для простых типов:**
  - [`StrParamWidget`](ui/param_widgets.py:55) — QLineEdit для строк
  - [`IntParamWidget`](ui/param_widgets.py:104) — QSpinBox для целых чисел с min/max
  - [`FloatParamWidget`](ui/param_widgets.py:163) — QDoubleSpinBox для дробных чисел с decimals
  - [`BoolParamWidget`](ui/param_widgets.py:226) — QCheckBox для булевых значений

- **Виджеты для специализированных типов:**
  - [`TimeParamWidget`](ui/param_widgets.py:263) — QTimeEdit для времени (минуты от полуночи)
  - [`SelectParamWidget`](ui/param_widgets.py:321) — QComboBox для выбора из списка
  - [`TickerParamWidget`](ui/param_widgets.py:370) — обёртка над TickerSelector
  - [`InstrumentsParamWidget`](ui/param_widgets.py:410) — обёртка над _InstrumentsWidget

- **[`ParamWidgetFactory`](ui/param_widgets.py:495)** — фабрика для создания виджетов
  - `create(key, meta, current_value, connector_id)` — создаёт виджет по типу
  - `register(type_name, widget_class)` — регистрирует новый тип виджета
  - Поддержка типов: str, int, float, bool, time, select, choice, ticker, instruments

### 2. [`docs/strategy_params_guide.md`](docs/strategy_params_guide.md:1) — Руководство по параметрам

Подробная документация на русском языке:
- Описание всех поддерживаемых типов параметров
- Примеры определения параметров в стратегиях
- Инструкция по добавлению новых типов виджетов
- Описание валидации и специальных случаев
- Примеры из реальных стратегий проекта

### 3. Планы и архитектура

- [`plans/auto_params_ui_generation.md`](plans/auto_params_ui_generation.md:1) — архитектурный документ с диаграммами
- [`plans/implementation_tasks.md`](plans/implementation_tasks.md:1) — разбивка на атомарные задачи

---

## Изменённые файлы

### [`ui/strategy_window.py`](ui/strategy_window.py:1)

**Метод [`tab_params()`](ui/strategy_window.py:528)** (строки 528-590):
- **Было:** ~105 строк с большим блоком if/elif для каждого типа параметра
- **Стало:** ~60 строк с использованием `ParamWidgetFactory.create()`
- Сохранена специальная обработка ticker с board для QUIK
- Код стал чище и расширяемее

**Метод [`save_params()`](ui/strategy_window.py:1096)** (строки 1096-1150):
- Использует `widget.get_value()` для всех `BaseParamWidget`
- Специальная обработка `TickerParamWidget.get_board()`
- Fallback на старую логику для обратной совместимости

**Метод [`_sync_tickers()`](ui/strategy_window.py:260)** (строки 260-300):
- Поддержка как `TickerParamWidget`, так и старого `TickerSelector`
- Извлечение внутреннего `ticker_selector` из `TickerParamWidget`
- Сохранена синхронизация между вкладками

---

## Преимущества

### 1. Автоматизация
- UI генерируется автоматически из схемы параметров `get_params()`
- Не нужно писать код для создания виджетов вручную
- Единообразие интерфейса для всех стратегий

### 2. Расширяемость
- Новые типы параметров добавляются через `ParamWidgetFactory.register()`
- Не нужно модифицировать `strategy_window.py`
- Пример добавления нового типа в документации

### 3. Надёжность
- Единая точка валидации параметров
- Автоматическое применение min/max/step из метаданных
- Встроенная валидация для каждого типа

### 4. Обратная совместимость
- Все существующие стратегии работают без изменений
- Схема параметров `get_params()` остаётся прежней
- Fallback на старую логику при необходимости

---

## Поддерживаемые типы параметров

| Тип | Виджет | Метаданные | Пример использования |
|-----|--------|------------|----------------------|
| `str` | QLineEdit | required, min_length, max_length | Название, описание |
| `int` | QSpinBox | min, max, step | Период индикатора, лотность |
| `float` | QDoubleSpinBox | min, max, step, decimals | Коэффициенты, проценты |
| `bool` | QCheckBox | - | Флаги включения/выключения |
| `time` | QTimeEdit | - | Время входа/выхода (минуты) |
| `select`/`choice` | QComboBox | options, labels | Тип заявки, режим работы |
| `ticker` | TickerSelector | - | Торгуемый инструмент |
| `instruments` | InstrumentsWidget | - | Корзина инструментов |

---

## Примеры использования

### До (старый способ)

```python
# В strategy_window.py нужно было вручную добавлять код
if ptype == "int":
    widget = QSpinBox()
    widget.setRange(meta.get("min", 0), meta.get("max", 1_000_000))
    widget.setValue(int(current))
    widget.setFixedWidth(120)
elif ptype == "float":
    widget = QDoubleSpinBox()
    widget.setRange(meta.get("min", 0.0), meta.get("max", 1_000_000.0))
    widget.setDecimals(meta.get("decimals", 2))
    widget.setValue(float(current))
    widget.setFixedWidth(120)
# ... ещё 80+ строк для других типов
```

### После (новый способ)

```python
# Просто создаём виджет через фабрику
widget = ParamWidgetFactory.create(key, meta, current, connector_id)
form.addRow(f"{meta.get('label', key)}:", widget)
```

### Добавление нового типа параметра

```python
# 1. Создаём класс виджета
class ColorParamWidget(BaseParamWidget):
    def __init__(self, key, meta, current_value, connector_id=None):
        super().__init__(key, meta, current_value)
        self.color_button = QPushButton()
        self.color_button.clicked.connect(self._choose_color)
        # ... логика выбора цвета
    
    def get_value(self):
        return self.selected_color
    
    def set_value(self, value):
        self.selected_color = value
        self._update_button_color()

# 2. Регистрируем в фабрике
ParamWidgetFactory.register("color", ColorParamWidget)

# 3. Используем в стратегии
def get_params():
    return {
        "line_color": {
            "type": "color",
            "default": "#89b4fa",
            "label": "Цвет линии",
            "description": "Цвет линии индикатора на графике"
        }
    }
```

---

## Миграция существующих стратегий

**Не требуется!** Все существующие стратегии продолжают работать без изменений:

- ✅ [`strategies/daytrend.py`](strategies/daytrend.py:21) — int, float, time
- ✅ [`strategies/valera_trend.py`](strategies/valera_trend.py:20) — int, time, ticker
- ✅ [`strategies/tracker.py`](strategies/tracker.py:21) — int, float, time, ticker
- ✅ [`strategies/achilles.py`](strategies/achilles.py:47) — time, float, select, instruments

---

## Тестирование

Система протестирована на всех существующих стратегиях:
- Корректное отображение всех типов параметров
- Сохранение и загрузка значений
- Валидация min/max для числовых типов
- Синхронизация тикеров между вкладками
- Блокировка редактирования при активной стратегии

---

## Дальнейшее развитие

### Возможные улучшения:

1. **Условная видимость параметров**
   ```python
   "stop_loss_value": {
       "type": "float",
       "visible_if": {"use_stop_loss": True}  # показывать только если use_stop_loss=True
   }
   ```

2. **Группировка параметров**
   ```python
   "sma_period": {
       "type": "int",
       "group": "Индикаторы",  # группировка в UI
       "label": "Период SMA"
   }
   ```

3. **Дополнительные типы виджетов**
   - `date` — выбор даты
   - `datetime` — выбор даты и времени
   - `slider` — ползунок для числовых значений
   - `multiselect` — множественный выбор
   - `file_path` — выбор файла

---

## Заключение

Реализована полностью автоматическая система генерации UI для параметров стратегий. Система:
- **Автоматическая** — UI генерируется из схемы параметров
- **Расширяемая** — новые типы добавляются без изменения существующего кода
- **Надёжная** — единая точка валидации и обработки параметров
- **Удобная** — разработчику стратегии достаточно описать параметры в `get_params()`

При добавлении новой стратегии теперь не нужно писать код UI — достаточно правильно описать параметры в функции `get_params()`, и интерфейс сгенерируется автоматически.
