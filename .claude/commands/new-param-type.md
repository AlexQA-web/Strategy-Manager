# /new-param-type

Добавь новый тип параметра стратегии в систему автогенерации UI.

## Входные данные

Спроси (если не указано):
1. Название типа (eng., lowercase): например `"color"`, `"slider"`, `"date"`
2. Описание: что делает виджет
3. Тип возвращаемого значения из `get_value()`

## Что создать

### 1. Класс виджета в `ui/param_widgets.py`

```python
class <N>ParamWidget(BaseParamWidget):
    """Виджет для параметра типа <type>"""

    def __init__(self, key: str, meta: dict, current_value: Any,
                 connector_id: str = None, parent=None):
        super().__init__(key, meta, current_value, connector_id, parent)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        # --- создать Qt-виджет ---
        self.<widget> = Q<Widget>(self)
        lay.addWidget(self.<widget>)

        # --- установить начальное значение ---
        val = current_value if current_value is not None else meta.get("default")
        if val is not None:
            self.set_value(val)

        if self.toolTip():
            self.<widget>.setToolTip(self.toolTip())

    def get_value(self) -> <ReturnType>:
        """Возвращает текущее значение"""
        return ...

    def set_value(self, value: Any):
        """Устанавливает значение"""
        try:
            ...
        except (ValueError, TypeError):
            pass

    def validate(self) -> Tuple[bool, str]:
        """Валидация"""
        value = self.get_value()
        # проверки...
        return True, ""
```

### 2. Зарегистрировать в `ui/param_widgets.py`

В блоке регистрации внизу файла добавить:
```python
ParamWidgetFactory.register("<type>", <N>ParamWidget)
```

### 3. Обновить документацию в `docs/strategy_params_guide.md`

Добавить строку в таблицу типов:
```markdown
| `<type>` | <WidgetDescription> | <description> | `default`, ... |
```

## Правила виджета

- Наследовать только от `BaseParamWidget` (не от Qt-виджетов напрямую)
- `get_value()` — всегда возвращает конкретный тип, не `Any`
- `set_value()` — не бросает исключения, обрабатывает некорректный input
- `validate()` — возвращает `(True, "")` при валидном значении
- Если виджет содержит QSpinBox/QDoubleSpinBox/QComboBox — добавить `setFocusPolicy(StrongFocus)` и игнор `wheelEvent` без фокуса (по аналогии с `_NoScrollSpinBox`)
- Ширина фиксированная через `setFixedWidth()`, не растягивается

## Пример использования в стратегии

```python
def get_params() -> dict:
    return {
        "my_param": {
            "type": "<type>",
            "default": <default_value>,
            "label": "Название параметра",
            "description": "Описание для tooltip",
            # доп. мета-поля...
        }
    }
```

## Проверка

```bash
python -m py_compile ui/param_widgets.py
```
