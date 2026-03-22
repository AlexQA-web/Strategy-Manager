# Руководство по работе с параметрами стратегий

## 1. Введение

Система параметров стратегий в Trading Manager обеспечивает автоматическую генерацию пользовательского интерфейса на основе метаданных, определённых в методе [`get_params()`](../strategies/daytrend.py:21) стратегии. Это позволяет разработчикам стратегий сосредоточиться на торговой логике, не беспокоясь о создании UI-элементов.

### Как это работает

1. **Определение параметров**: В методе [`get_params()`](../strategies/daytrend.py:21) стратегии вы описываете параметры с их типами и метаданными
2. **Автоматическая генерация UI**: Система автоматически создаёт соответствующие виджеты (поля ввода, чекбоксы, выпадающие списки и т.д.)
3. **Валидация**: Встроенная валидация проверяет корректность введённых значений
4. **Сохранение**: Значения параметров автоматически сохраняются и загружаются

### Архитектура

- **[`BaseParamWidget`](../ui/param_widgets.py:14)** — базовый абстрактный класс для всех виджетов параметров
- **[`ParamWidgetFactory`](../ui/param_widgets.py:495)** — фабрика для создания виджетов по типу параметра
- **Специализированные виджеты** — классы для каждого типа параметра (StrParamWidget, IntParamWidget и т.д.)

---

## 2. Поддерживаемые типы параметров

| Тип | Виджет UI | Описание | Основные метаданные |
|-----|-----------|----------|---------------------|
| **str** | [`QLineEdit`](../ui/param_widgets.py:56) | Текстовое поле для строк | `default`, `required`, `min_length`, `max_length` |
| **int** | [`QSpinBox`](../ui/param_widgets.py:105) | Числовое поле для целых чисел | `default`, `min`, `max`, `step` |
| **float** | [`QDoubleSpinBox`](../ui/param_widgets.py:164) | Числовое поле с плавающей точкой | `default`, `min`, `max`, `step`, `decimals` |
| **bool** | [`QCheckBox`](../ui/param_widgets.py:227) | Чекбокс для булевых значений | `default` |
| **time** | [`QTimeEdit`](../ui/param_widgets.py:263) | Выбор времени (минуты от полуночи) | `default`, `min`, `max` |
| **select** / **choice** | [`QComboBox`](../ui/param_widgets.py:327) | Выпадающий список | `default`, `options`, `labels` |
| **timeframe** | [`QComboBox`](../ui/param_widgets.py:509) | Выбор таймфрейма (1m, 5m, 15m, 30m, 1h, 4h, 1d) | `default`, `options` |
| **ticker** | [`TickerSelector`](../ui/param_widgets.py:383) | Выбор тикера с автодополнением | `default`, `board` |
| **instruments** | [`InstrumentsWidget`](../ui/param_widgets.py:431) | Список инструментов для корзинных стратегий | `default`, `min_items`, `max_items` |
| **commission** | [`CommissionWidget`](../ui/param_widgets.py:575) | Комиссия с автопереключением % / рубли | `default` |

### Общие метаданные для всех типов

- **`type`** (обязательно) — тип параметра из таблицы выше
- **`default`** — значение по умолчанию
- **`label`** — отображаемое название параметра в UI
- **`description`** — подсказка (tooltip), отображается при наведении

---

## 3. Примеры определения параметров

### 3.1. Строковый параметр (str)

```python
def get_params() -> dict:
    return {
        "ticker": {
            "type": "str",
            "default": "SiH6",
            "label": "Тикер",
            "description": "Торгуемый инструмент",
            "required": True,  # Обязательное поле
            "min_length": 2,   # Минимум 2 символа
            "max_length": 10   # Максимум 10 символов
        }
    }
```

**Виджет**: Текстовое поле шириной 200px  
**Валидация**: Проверка на пустоту (если `required`), длину строки

### 3.2. Целочисленный параметр (int)

```python
def get_params() -> dict:
    return {
        "qty": {
            "type": "int",
            "default": 1,
            "min": 1,
            "max": 100,
            "step": 1,
            "label": "Лот",
            "description": "Количество контрактов"
        }
    }
```

**Виджет**: QSpinBox с кнопками +/-  
**Валидация**: Автоматическое ограничение диапазона [min, max]

### 3.3. Параметр с плавающей точкой (float)

```python
def get_params() -> dict:
    return {
        "k_long": {
            "type": "float",
            "default": 0.5,
            "min": 0.0,
            "max": 5.0,
            "step": 0.05,
            "decimals": 2,  # Количество знаков после запятой
            "label": "K лонга",
            "description": "Коэффициент расширения диапазона"
        }
    }
```

**Виджет**: QDoubleSpinBox с настраиваемой точностью  
**Валидация**: Ограничение диапазона и точности

### 3.4. Булев параметр (bool)

```python
def get_params() -> dict:
    return {
        "use_trailing_stop": {
            "type": "bool",
            "default": False,
            "label": "Трейлинг-стоп",
            "description": "Использовать трейлинг-стоп"
        }
    }
```

**Виджет**: QCheckBox  
**Валидация**: Не требуется (всегда True или False)

### 3.5. Параметр времени (time)

```python
def get_params() -> dict:
    return {
        "time_open": {
            "type": "time",
            "default": 600,  # 10:00 (600 минут от полуночи)
            "min": 0,
            "max": 1439,     # 23:59
            "label": "Время входа",
            "description": "Время входа в минутах от полуночи (600 = 10:00)"
        }
    }
```

**Виджет**: QTimeEdit с форматом HH:mm  
**Значение**: Хранится как целое число — минуты от полуночи (0-1439)  
**Конвертация**: 600 → 10:00, 1425 → 23:45

### 3.6. Выпадающий список (select/choice)

```python
def get_params() -> dict:
    return {
        "order_mode": {
            "type": "select",
            "default": "limit_book",
            "options": ["market", "limit_book", "limit_price"],
            "labels": {
                "market": "Рыночная",
                "limit_book": "Лимитка (Стакан)",
                "limit_price": "Лимитка (Цена)"
            },
            "label": "Тип заявки",
            "description": "Режим выставления ордеров"
        }
    }
```

**Виджет**: QComboBox  
**`options`**: Список возможных значений (сохраняются в параметрах)
**`labels`**: Словарь для отображения читаемых названий в UI

### 3.7. Таймфрейм (timeframe)

```python
def get_params() -> dict:
    return {
        "timeframe": {
            "type": "timeframe",
            "default": "5m",
            "label": "Таймфрейм",
            "description": "Временной интервал для работы стратегии"
        }
    }
```

**Виджет**: QComboBox с предопределенными таймфреймами
**Доступные значения**: `1m`, `5m`, `15m`, `30m`, `1h`, `4h`, `1d`
**Отображение**: Человекочитаемые названия (1 минута, 5 минут, 1 час и т.д.)

**Использование в стратегии**:

```python
def on_bar(bars: list[dict], position: int, params: dict) -> dict:
    timeframe = params.get("timeframe", "5m")
    # Логика стратегии с учётом таймфрейма...
```

### 3.8. Выбор тикера (ticker)

```python
def get_params() -> dict:
    return {
        "ticker": {
            "type": "ticker",
            "default": "SBER",
            "board": "TQBR",  # Режим торгов для QUIK
            "label": "Тикер",
            "description": "Торгуемый инструмент"
        }
    }
```

**Виджет**: TickerSelector с автодополнением и поиском  
**Особенности**: 
- Интеграция с MOEX API для получения списка инструментов
- Поддержка board для QUIK (TQBR, SPBFUT и т.д.)
- Автодополнение при вводе

### 3.8. Корзина инструментов (instruments)

```python
def get_params() -> dict:
    return {
        "instruments": {
            "type": "instruments",
            "default": [
                {"ticker": "SBER", "board": "TQBR", "allow_buy": True, "allow_sell": False},
                {"ticker": "GAZP", "board": "TQBR", "allow_buy": True, "allow_sell": True},
                {"ticker": "LKOH", "board": "TQBR", "allow_buy": True, "allow_sell": True}
            ],
            "min_items": 1,   # Минимум 1 инструмент
            "max_items": 20,  # Максимум 20 инструментов
            "label": "Инструменты",
            "description": "Корзина инструментов с разрешениями на покупку/продажу"
        }
    }
```

**Виджет**: Таблица с кнопками добавления/удаления инструментов  
**Структура элемента**:
- `ticker` — код инструмента
- `board` — режим торгов (для QUIK)
- `allow_buy` — разрешить покупку
- `allow_sell` — разрешить продажу

---

## 4. Как добавить новый тип параметра

### Шаг 1: Создание класса виджета

Создайте класс, наследующий [`BaseParamWidget`](../ui/param_widgets.py:14):

```python
from ui.param_widgets import BaseParamWidget
from PyQt6.QtWidgets import QSlider
from PyQt6.QtCore import Qt
from typing import Any, Tuple


class SliderParamWidget(BaseParamWidget):
    """Виджет для параметра-слайдера"""
    
    def __init__(self, key: str, meta: dict, current_value: Any, 
                 connector_id: str = None, parent=None):
        super().__init__(key, meta, current_value, connector_id, parent)
        
        # Создаём слайдер
        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setFixedWidth(200)
        
        # Устанавливаем диапазон из метаданных
        min_val = meta.get("min", 0)
        max_val = meta.get("max", 100)
        self.slider.setRange(min_val, max_val)
        
        # Устанавливаем шаг
        step = meta.get("step", 1)
        self.slider.setSingleStep(step)
        
        # Устанавливаем текущее значение
        if current_value is not None:
            self.slider.setValue(int(current_value))
        else:
            default = meta.get("default", min_val)
            self.slider.setValue(int(default))
        
        # Применяем tooltip
        if self.toolTip():
            self.slider.setToolTip(self.toolTip())
    
    def get_value(self) -> int:
        """Возвращает текущее значение слайдера"""
        return self.slider.value()
    
    def set_value(self, value: Any):
        """Устанавливает значение слайдера"""
        try:
            self.slider.setValue(int(value))
        except (ValueError, TypeError):
            pass
    
    def validate(self) -> Tuple[bool, str]:
        """Валидация значения слайдера"""
        value = self.get_value()
        
        min_val = self.meta.get("min")
        if min_val is not None and value < min_val:
            return False, f"Значение должно быть >= {min_val}"
        
        max_val = self.meta.get("max")
        if max_val is not None and value > max_val:
            return False, f"Значение должно быть <= {max_val}"
        
        return True, ""
```

### Шаг 2: Регистрация в фабрике

Добавьте регистрацию в конец файла [`param_widgets.py`](../ui/param_widgets.py:541):

```python
# В конце файла ui/param_widgets.py
ParamWidgetFactory.register("slider", SliderParamWidget)
```

### Шаг 3: Использование в стратегии

Теперь можно использовать новый тип в [`get_params()`](../strategies/daytrend.py:21):

```python
def get_params() -> dict:
    return {
        "risk_level": {
            "type": "slider",
            "default": 50,
            "min": 0,
            "max": 100,
            "step": 5,
            "label": "Уровень риска",
            "description": "Уровень риска от 0 до 100"
        }
    }
```

### Требования к классу виджета

Обязательные методы:
- **`__init__(key, meta, current_value, connector_id, parent)`** — конструктор
- **[`get_value()`](../ui/param_widgets.py:37)** → Any — возвращает текущее значение
- **[`set_value(value)`](../ui/param_widgets.py:41)** — устанавливает значение
- **[`validate()`](../ui/param_widgets.py:46)** → (bool, str) — валидация (True/"" если OK)

---

## 5. Валидация параметров

### Встроенная валидация

Каждый виджет реализует метод [`validate()`](../ui/param_widgets.py:46), который вызывается перед сохранением параметров:

```python
def validate(self) -> Tuple[bool, str]:
    """
    Returns:
        (is_valid, error_message): True если валидно, иначе False с сообщением
    """
    value = self.get_value()
    
    # Проверка диапазона
    min_val = self.meta.get("min")
    if min_val is not None and value < min_val:
        return False, f"Значение должно быть >= {min_val}"
    
    max_val = self.meta.get("max")
    if max_val is not None and value > max_val:
        return False, f"Значение должно быть <= {max_val}"
    
    return True, ""
```

### Примеры валидации по типам

**Строки ([`StrParamWidget`](../ui/param_widgets.py:56))**:
- Проверка на обязательность (`required`)
- Минимальная длина (`min_length`)
- Максимальная длина (`max_length`)

**Числа ([`IntParamWidget`](../ui/param_widgets.py:105), [`FloatParamWidget`](../ui/param_widgets.py:164))**:
- Диапазон значений (`min`, `max`)
- Автоматическое ограничение в UI

**Время ([`TimeParamWidget`](../ui/param_widgets.py:263))**:
- Диапазон времени в минутах от полуночи

**Выбор ([`SelectParamWidget`](../ui/param_widgets.py:327))**:
- Значение должно быть из списка `options`

**Инструменты ([`InstrumentsParamWidget`](../ui/param_widgets.py:431))**:
- Минимальное количество (`min_items`)
- Максимальное количество (`max_items`)

### Обработка ошибок валидации

При ошибке валидации пользователю показывается диалог с описанием проблемы:

```python
# Пример из strategy_window.py
is_valid, error_msg = widget.validate()
if not is_valid:
    QMessageBox.warning(
        self,
        "Ошибка валидации",
        f"Параметр '{key}': {error_msg}"
    )
    return
```

---

## 6. Специальные случаи

### 6.1. Параметр ticker с board для QUIK

Для стратегий, работающих через QUIK, необходимо указывать режим торгов (board):

```python
def get_params() -> dict:
    return {
        "ticker": {
            "type": "ticker",
            "default": "SiH6",
            "board": "SPBFUT",  # Фьючерсы
            "label": "Тикер",
            "description": "Торгуемый инструмент"
        }
    }
```

**Популярные board**:
- `TQBR` — акции основного режима
- `SPBFUT` — фьючерсы
- `CETS` — валютный рынок
- `TQTF` — ETF

### 6.2. Параметр instruments для корзинных стратегий

Используется в стратегиях типа mean reversion, где торгуется корзина инструментов:

```python
def get_params() -> dict:
    return {
        "instruments": {
            "type": "instruments",
            "default": [
                {"ticker": "SBER", "board": "TQBR", "allow_buy": True, "allow_sell": False},
                {"ticker": "GAZP", "board": "TQBR", "allow_buy": True, "allow_sell": True}
            ],
            "label": "Инструменты",
            "description": "Корзина инструментов"
        }
    }
```

**Доступ к данным в стратегии**:

```python
def on_bar(bars: list[dict], position: int, params: dict) -> dict:
    instruments = params.get("instruments", [])
    
    for instr in instruments:
        ticker = instr["ticker"]
        board = instr.get("board", "TQBR")
        allow_buy = instr.get("allow_buy", True)
        allow_sell = instr.get("allow_sell", True)
        
        # Логика стратегии...
```

### 6.3. Синхронизация тикеров между вкладками

При изменении тикера в параметрах стратегии автоматически обновляется:
- Вкладка "Параметры"
- Вкладка "Бэктест"
- Вкладка "График"

Это реализовано через сигналы PyQt6:

```python
# В strategy_window.py
self.ticker_changed.connect(self.on_ticker_changed)

def on_ticker_changed(self, new_ticker: str):
    # Обновляем все вкладки
    self.backtest_tab.set_ticker(new_ticker)
    self.chart_tab.set_ticker(new_ticker)
```

---

## 7. Примеры из существующих стратегий

### 7.1. DayTrend — простые типы

Файл: [`strategies/daytrend.py`](../strategies/daytrend.py:21)

```python
def get_params() -> dict:
    return {
        "ticker": {
            "type": "str", "default": "SiZ5",
            "label": "Тикер",
            "description": "Торгуемый инструмент",
        },
        "k_long": {
            "type": "float", "default": 0.5, "min": 0.0, "max": 5.0, "step": 0.05,
            "label": "K лонга",
            "description": "Коэффициент расширения диапазона для входа в лонг",
        },
        "stop_long": {
            "type": "float", "default": 100.0, "min": 0.0, "max": 10000.0, "step": 10.0,
            "label": "Стоп лонг",
            "description": "Отступ стопа для лонга (пункты от Low[-1])",
        },
        "qty": {
            "type": "int", "default": 1, "min": 1, "max": 100,
            "label": "Лот",
            "description": "Кол-во контрактов",
        },
        "time_start": {
            "type": "int", "default": 605, "min": 0, "max": 1439,
            "label": "Время входа (мин)",
            "description": "Начало торговли в минутах от полуночи (605 = 10:05)",
        }
    }
```

**Используемые типы**: str, float, int  
**Особенности**: Простые параметры с валидацией диапазонов

### 7.2. Valera Trend — параметры времени

Файл: [`strategies/valera_trend.py`](../strategies/valera_trend.py:20)

```python
def get_params() -> dict:
    return {
        "ticker": {
            "type": "str", "default": "SiH6",
            "label": "Тикер",
            "description": "Торгуемый инструмент",
        },
        "sma_period": {
            "type": "int", "default": 200, "min": 5, "max": 2000,
            "label": "Период SMA",
            "description": "Период скользящей средней",
        },
        "time_open": {
            "type": "time", "default": 600,
            "label": "Время входа (мин)",
            "description": "Время входа в минутах от полуночи (600 = 10:00)",
        },
        "time_close": {
            "type": "time", "default": 1425,
            "label": "Время выхода (мин)",
            "description": "Время выхода в минутах от полуночи (1425 = 23:45)",
        }
    }
```

**Используемые типы**: str, int, time  
**Особенности**: Использование типа `time` для временных параметров

### 7.3. Tracker — комбинация типов

Файл: [`strategies/tracker.py`](../strategies/tracker.py:21)

```python
def get_params() -> dict:
    return {
        "ticker": {
            "type": "str", "default": "SiH6",
            "label": "Тикер",
            "description": "Торгуемый инструмент",
        },
        "compress_tf": {
            "type": "int", "default": 15, "min": 1, "max": 240,
            "label": "Старший ТФ (мин)",
            "description": "Таймфрейм для SMA и ATR (минуты)",
        },
        "k": {
            "type": "float", "default": 1.0, "min": 0.1, "max": 10.0, "step": 0.1,
            "label": "К (множитель ATR)",
            "description": "Ширина канала: SMA ± ATR * K",
        },
        "time_open": {
            "type": "time", "default": 630,
            "label": "Время входа (мин)",
            "description": "Начало торговли в минутах от полуночи (630 = 10:30)",
        },
        "friday_close": {
            "type": "time", "default": 1005,
            "label": "Закрытие в пятницу (мин)",
            "description": "Принудительное закрытие в пятницу (1005 = 16:45)",
        }
    }
```

**Используемые типы**: str, int, float, time  
**Особенности**: Комбинация различных типов для сложной стратегии

### 7.4. Achilles — instruments и select

Файл: [`strategies/achilles.py`](../strategies/achilles.py:47)

```python
def get_params() -> dict:
    return {
        "time_snapshot": {
            "type": "time",
            "default": 600,
            "label": "Время снимка",
            "description": "Время фиксации референсных цен",
        },
        "spread_offset": {
            "type": "float",
            "default": 2.0,
            "min": 0.0,
            "max": 100.0,
            "label": "Отступ от стакана",
            "description": "Отступ от bid/ask при лимитных заявках",
        },
        "order_mode": {
            "type": "select",
            "default": "limit_book",
            "options": ["market", "limit_book", "limit_price"],
            "labels": {
                "market": "Рыночная",
                "limit_book": "Лимитка (Стакан)",
                "limit_price": "Лимитка (Цена)"
            },
            "label": "Тип заявки",
            "description": "Режим выставления ордеров",
        },
        "instruments": {
            "type": "instruments",
            "default": [
                {"ticker": "SiH6", "board": "SPBFUT", "allow_buy": True, "allow_sell": True},
                {"ticker": "SBER", "board": "TQBR", "allow_buy": True, "allow_sell": False},
                {"ticker": "GAZP", "board": "TQBR", "allow_buy": True, "allow_sell": True}
            ],
            "label": "Инструменты",
            "description": "Корзина инструментов с разрешениями на покупку/продажу",
        }
    }
```

**Используемые типы**: time, float, select, instruments  
**Особенности**: 
- Использование `select` для выбора режима ордеров
- Корзина инструментов с индивидуальными настройками
- Комплексная стратегия mean reversion

---

## Заключение

Система параметров стратегий обеспечивает:
- ✅ Автоматическую генерацию UI из метаданных
- ✅ Встроенную валидацию значений
- ✅ Расширяемость через добавление новых типов
- ✅ Единообразный интерфейс для всех стратегий
- ✅ Автоматическое сохранение и загрузку параметров

Для добавления параметров в стратегию достаточно описать их в методе [`get_params()`](../strategies/daytrend.py:21) — всё остальное система сделает автоматически.
