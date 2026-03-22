"""
Виджеты параметров стратегий с автоматической генерацией UI
"""
from abc import ABCMeta, abstractmethod
from typing import Any, Tuple

from PyQt6.QtWidgets import (
    QWidget, QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox,
    QComboBox, QTimeEdit, QHBoxLayout
)
from PyQt6.QtCore import Qt, QTime


class QABCMeta(type(QWidget), ABCMeta):
    """Метакласс для совместимости QWidget и ABC"""
    pass


class BaseParamWidget(QWidget, metaclass=QABCMeta):
    """Базовый абстрактный класс для виджета параметра стратегии"""
    
    def __init__(self, key: str, meta: dict, current_value: Any, connector_id: str = None, parent=None):
        """
        Args:
            key: Ключ параметра
            meta: Метаданные параметра (type, min, max, description и т.д.)
            current_value: Текущее значение параметра
            connector_id: ID коннектора (для специфичных виджетов)
            parent: Родительский виджет
        """
        super().__init__(parent)
        self.key = key
        self.meta = meta
        self.connector_id = connector_id
        
        # Устанавливаем tooltip из описания
        description = meta.get("description", "")
        if description:
            self.setToolTip(description)
    
    @abstractmethod
    def get_value(self) -> Any:
        """Возвращает текущее значение параметра"""
        pass
    
    @abstractmethod
    def set_value(self, value: Any):
        """Устанавливает значение параметра"""
        pass
    
    def validate(self) -> Tuple[bool, str]:
        """
        Валидация текущего значения
        
        Returns:
            (is_valid, error_message): True если валидно, иначе False с сообщением об ошибке
        """
        return True, ""


class StrParamWidget(BaseParamWidget):
    """Виджет для строковых параметров (QLineEdit)"""
    
    def __init__(self, key: str, meta: dict, current_value: Any, connector_id: str = None, parent=None):
        super().__init__(key, meta, current_value, connector_id, parent)
        
        self.line_edit = QLineEdit(self)
        self.line_edit.setFixedWidth(200)
        
        # Устанавливаем текущее значение
        if current_value is not None:
            self.line_edit.setText(str(current_value))
        else:
            default = meta.get("default", "")
            self.line_edit.setText(str(default))
        
        # Применяем tooltip к самому виджету ввода
        if self.toolTip():
            self.line_edit.setToolTip(self.toolTip())
    
    def get_value(self) -> str:
        """Возвращает текущее значение строки"""
        return self.line_edit.text()
    
    def set_value(self, value: Any):
        """Устанавливает значение строки"""
        self.line_edit.setText(str(value) if value is not None else "")
    
    def validate(self) -> Tuple[bool, str]:
        """Валидация строки"""
        value = self.get_value()
        
        # Проверка на обязательность
        if self.meta.get("required", False) and not value.strip():
            return False, f"Параметр '{self.key}' обязателен для заполнения"
        
        # Проверка минимальной длины
        min_length = self.meta.get("min_length")
        if min_length is not None and len(value) < min_length:
            return False, f"Минимальная длина: {min_length} символов"
        
        # Проверка максимальной длины
        max_length = self.meta.get("max_length")
        if max_length is not None and len(value) > max_length:
            return False, f"Максимальная длина: {max_length} символов"
        
        return True, ""


class IntParamWidget(BaseParamWidget):
    """Виджет для целочисленных параметров (QSpinBox)"""
    
    def __init__(self, key: str, meta: dict, current_value: Any, connector_id: str = None, parent=None):
        super().__init__(key, meta, current_value, connector_id, parent)
        
        self.spin_box = QSpinBox(self)
        self.spin_box.setFixedWidth(120)
        
        # Устанавливаем диапазон из метаданных
        min_val = meta.get("min", 0)
        max_val = meta.get("max", 1_000_000)
        self.spin_box.setRange(min_val, max_val)
        
        # Устанавливаем шаг
        step = meta.get("step", 1)
        self.spin_box.setSingleStep(step)
        
        # Устанавливаем текущее значение
        if current_value is not None:
            try:
                self.spin_box.setValue(int(current_value))
            except (ValueError, TypeError):
                default = meta.get("default", min_val)
                self.spin_box.setValue(int(default))
        else:
            default = meta.get("default", min_val)
            self.spin_box.setValue(int(default))
        
        # Применяем tooltip
        if self.toolTip():
            self.spin_box.setToolTip(self.toolTip())
    
    def get_value(self) -> int:
        """Возвращает текущее целочисленное значение"""
        return self.spin_box.value()
    
    def set_value(self, value: Any):
        """Устанавливает целочисленное значение"""
        try:
            self.spin_box.setValue(int(value))
        except (ValueError, TypeError):
            pass
    
    def validate(self) -> Tuple[bool, str]:
        """Валидация целочисленного значения"""
        value = self.get_value()
        
        min_val = self.meta.get("min")
        if min_val is not None and value < min_val:
            return False, f"Значение должно быть >= {min_val}"
        
        max_val = self.meta.get("max")
        if max_val is not None and value > max_val:
            return False, f"Значение должно быть <= {max_val}"
        
        return True, ""


class FloatParamWidget(BaseParamWidget):
    """Виджет для параметров с плавающей точкой (QDoubleSpinBox)"""
    
    def __init__(self, key: str, meta: dict, current_value: Any, connector_id: str = None, parent=None):
        super().__init__(key, meta, current_value, connector_id, parent)
        
        self.spin_box = QDoubleSpinBox(self)
        self.spin_box.setFixedWidth(120)
        
        # Устанавливаем количество знаков после запятой
        decimals = meta.get("decimals", 2)
        self.spin_box.setDecimals(decimals)
        
        # Устанавливаем диапазон из метаданных
        min_val = meta.get("min", 0.0)
        max_val = meta.get("max", 1_000_000.0)
        self.spin_box.setRange(min_val, max_val)
        
        # Устанавливаем шаг
        step = meta.get("step", 0.1)
        self.spin_box.setSingleStep(step)
        
        # Устанавливаем текущее значение
        if current_value is not None:
            try:
                self.spin_box.setValue(float(current_value))
            except (ValueError, TypeError):
                default = meta.get("default", min_val)
                self.spin_box.setValue(float(default))
        else:
            default = meta.get("default", min_val)
            self.spin_box.setValue(float(default))
        
        # Применяем tooltip
        if self.toolTip():
            self.spin_box.setToolTip(self.toolTip())
    
    def get_value(self) -> float:
        """Возвращает текущее значение с плавающей точкой"""
        return self.spin_box.value()
    
    def set_value(self, value: Any):
        """Устанавливает значение с плавающей точкой"""
        try:
            self.spin_box.setValue(float(value))
        except (ValueError, TypeError):
            pass
    
    def validate(self) -> Tuple[bool, str]:
        """Валидация значения с плавающей точкой"""
        value = self.get_value()
        
        min_val = self.meta.get("min")
        if min_val is not None and value < min_val:
            return False, f"Значение должно быть >= {min_val}"
        
        max_val = self.meta.get("max")
        if max_val is not None and value > max_val:
            return False, f"Значение должно быть <= {max_val}"
        
        return True, ""


class BoolParamWidget(BaseParamWidget):
    """Виджет для булевых параметров (QCheckBox)"""
    
    def __init__(self, key: str, meta: dict, current_value: Any, connector_id: str = None, parent=None):
        super().__init__(key, meta, current_value, connector_id, parent)
        
        self.checkbox = QCheckBox(self)
        
        # Устанавливаем текущее значение
        if current_value is not None:
            if isinstance(current_value, bool):
                self.checkbox.setChecked(current_value)
            else:
                # Преобразуем строки "true"/"false" и числа 0/1
                self.checkbox.setChecked(bool(current_value))
        else:
            default = meta.get("default", False)
            self.checkbox.setChecked(bool(default))
        
        # Применяем tooltip
        if self.toolTip():
            self.checkbox.setToolTip(self.toolTip())
    
    def get_value(self) -> bool:
        """Возвращает текущее булево значение"""
        return self.checkbox.isChecked()
    
    def set_value(self, value: Any):
        """Устанавливает булево значение"""
        self.checkbox.setChecked(bool(value))
    
    def validate(self) -> Tuple[bool, str]:
        """Валидация булева значения (всегда валидно)"""
        return True, ""


class TimeParamWidget(BaseParamWidget):
    """Виджет для параметров типа time (минуты от полуночи)"""
    
    def __init__(self, key: str, meta: dict, current_value: Any, connector_id: str = None, parent=None):
        super().__init__(key, meta, current_value, connector_id, parent)
        
        self.time_edit = QTimeEdit(self)
        self.time_edit.setDisplayFormat("HH:mm")
        self.time_edit.setFixedWidth(120)
        
        # Конвертируем минуты от полуночи в QTime
        if current_value is not None:
            try:
                minutes = int(current_value)
                hours = minutes // 60
                mins = minutes % 60
                self.time_edit.setTime(QTime(hours, mins))
            except (ValueError, TypeError):
                default = meta.get("default", 0)
                minutes = int(default)
                hours = minutes // 60
                mins = minutes % 60
                self.time_edit.setTime(QTime(hours, mins))
        else:
            default = meta.get("default", 0)
            minutes = int(default)
            hours = minutes // 60
            mins = minutes % 60
            self.time_edit.setTime(QTime(hours, mins))
        
        # Применяем tooltip
        if self.toolTip():
            self.time_edit.setToolTip(self.toolTip())
    
    def get_value(self) -> int:
        """Возвращает минуты от полуночи"""
        time = self.time_edit.time()
        return time.hour() * 60 + time.minute()
    
    def set_value(self, value: Any):
        """Устанавливает время из минут от полуночи"""
        try:
            minutes = int(value)
            hours = minutes // 60
            mins = minutes % 60
            self.time_edit.setTime(QTime(hours, mins))
        except (ValueError, TypeError):
            pass
    
    def validate(self) -> Tuple[bool, str]:
        """Валидация времени"""
        value = self.get_value()
        
        min_val = self.meta.get("min")
        if min_val is not None and value < min_val:
            return False, f"Время должно быть >= {min_val // 60:02d}:{min_val % 60:02d}"
        
        max_val = self.meta.get("max")
        if max_val is not None and value > max_val:
            return False, f"Время должно быть <= {max_val // 60:02d}:{max_val % 60:02d}"
        
        return True, ""


class SelectParamWidget(BaseParamWidget):
    """Виджет для параметров типа select/choice (выпадающий список)"""
    
    def __init__(self, key: str, meta: dict, current_value: Any, connector_id: str = None, parent=None):
        super().__init__(key, meta, current_value, connector_id, parent)
        
        self.combo_box = QComboBox(self)
        self.combo_box.setFixedWidth(200)
        
        # Загружаем опции из метаданных
        options = meta.get("options", [])
        labels = meta.get("labels", {})
        
        # Если labels - список, преобразуем в словарь
        if isinstance(labels, list):
            labels = {opt: lbl for opt, lbl in zip(options, labels)} if len(labels) == len(options) else {}
        
        # Заполняем комбобокс
        for option in options:
            # Используем label если есть, иначе само значение
            label = labels.get(option, str(option)) if isinstance(labels, dict) else str(option)
            self.combo_box.addItem(label, option)
        
        # Устанавливаем текущее значение
        if current_value is not None:
            index = self.combo_box.findData(current_value)
            if index >= 0:
                self.combo_box.setCurrentIndex(index)
        else:
            default = meta.get("default")
            if default is not None:
                index = self.combo_box.findData(default)
                if index >= 0:
                    self.combo_box.setCurrentIndex(index)
        
        # Применяем tooltip
        if self.toolTip():
            self.combo_box.setToolTip(self.toolTip())
    
    def get_value(self) -> Any:
        """Возвращает выбранное значение (не индекс)"""
        return self.combo_box.currentData()
    
    def set_value(self, value: Any):
        """Устанавливает выбранное значение"""
        index = self.combo_box.findData(value)
        if index >= 0:
            self.combo_box.setCurrentIndex(index)
    
    def validate(self) -> Tuple[bool, str]:
        """Валидация выбранного значения"""
        value = self.get_value()
        options = self.meta.get("options", [])
        
        if value not in options:
            return False, f"Значение должно быть одним из: {', '.join(map(str, options))}"
        
        return True, ""


class TickerParamWidget(BaseParamWidget):
    """Виджет для параметра ticker - обёртка над TickerSelector"""
    
    def __init__(self, key: str, meta: dict, current_value: Any, connector_id: str = None, parent=None):
        super().__init__(key, meta, current_value, connector_id, parent)
        
        from ui.ticker_selector import TickerSelector
        from loguru import logger
        
        # Получаем текущий тикер и борд
        current_ticker = current_value if current_value else meta.get("default", "")
        current_board = meta.get("board", "TQBR")
        
        logger.debug(f"[TickerParamWidget] __init__: key={key}, connector_id={connector_id}, "
                     f"current_ticker={current_ticker}, current_board={current_board}")
        
        # Создаём TickerSelector
        self.ticker_selector = TickerSelector(
            connector_id=connector_id or "finam",
            current_ticker=current_ticker,
            current_board=current_board,
            parent=self
        )
        
        # Добавляем TickerSelector в layout виджета
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.ticker_selector)
        
        # Применяем tooltip
        if self.toolTip():
            self.ticker_selector.setToolTip(self.toolTip())
    
    def get_value(self) -> str:
        """Возвращает выбранный тикер"""
        return self.ticker_selector.ticker()
    
    def get_board(self) -> str:
        """Возвращает выбранный борд"""
        return self.ticker_selector.board()
    
    def set_value(self, value: Any):
        """Устанавливает тикер"""
        if value:
            board = self.meta.get("board", "TQBR")
            self.ticker_selector.set_ticker_and_board(str(value), board)
    
    def validate(self) -> Tuple[bool, str]:
        """Валидация тикера"""
        ticker = self.get_value()
        
        if self.meta.get("required", False) and not ticker.strip():
            return False, f"Параметр '{self.key}' обязателен для заполнения"
        
        return True, ""


class InstrumentsParamWidget(BaseParamWidget):
    """Виджет для параметра instruments - обёртка над _InstrumentsWidget"""
    
    def __init__(self, key: str, meta: dict, current_value: Any, connector_id: str = None, parent=None):
        super().__init__(key, meta, current_value, connector_id, parent)
        
        from ui.strategy_window import _InstrumentsWidget
        
        # Получаем текущий список инструментов
        instruments = current_value if isinstance(current_value, list) else []
        if not instruments:
            instruments = meta.get("default", [])
        
        # Создаём _InstrumentsWidget
        self.instruments_widget = _InstrumentsWidget(
            connector_id=connector_id or "finam",
            instruments=instruments,
            parent=self
        )
        
        # Применяем tooltip
        if self.toolTip():
            self.instruments_widget.setToolTip(self.toolTip())
    
    def get_value(self) -> list:
        """Возвращает список инструментов"""
        return self.instruments_widget.get_value()
    
    def set_value(self, value: Any):
        """Устанавливает список инструментов"""
        if isinstance(value, list):
            # Пересоздаём виджет с новым списком
            from ui.strategy_window import _InstrumentsWidget
            
            old_widget = self.instruments_widget
            self.instruments_widget = _InstrumentsWidget(
                connector_id=self.connector_id or "finam",
                instruments=value,
                parent=self
            )
            
            # Заменяем виджет в layout родителя
            # NOTE: self.layout() может вернуть None, поэтому используем parent.layout()
            parent_layout = self.parent().layout() if self.parent() else None
            if parent_layout:
                parent_layout.replaceWidget(old_widget, self.instruments_widget)
                old_widget.deleteLater()
    
    def validate(self) -> Tuple[bool, str]:
        """Валидация списка инструментов"""
        instruments = self.get_value()
        
        if self.meta.get("required", False) and not instruments:
            return False, f"Необходимо добавить хотя бы один инструмент"
        
        min_items = self.meta.get("min_items")
        if min_items is not None and len(instruments) < min_items:
            return False, f"Минимальное количество инструментов: {min_items}"
        
        max_items = self.meta.get("max_items")
        if max_items is not None and len(instruments) > max_items:
            return False, f"Максимальное количество инструментов: {max_items}"
        
        return True, ""


class TimeframeParamWidget(BaseParamWidget):
    """Виджет для параметра timeframe - выпадающий список с предопределенными таймфреймами"""
    
    # Стандартные таймфреймы, используемые в проекте
    TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
    
    # Человекочитаемые названия для таймфреймов
    TIMEFRAME_LABELS = {
        "1m": "1 минута",
        "5m": "5 минут",
        "15m": "15 минут",
        "30m": "30 минут",
        "1h": "1 час",
        "4h": "4 часа",
        "1d": "1 день",
    }
    
    def __init__(self, key: str, meta: dict, current_value: Any, connector_id: str = None, parent=None):
        super().__init__(key, meta, current_value, connector_id, parent)
        
        self.combo_box = QComboBox(self)
        self.combo_box.setFixedWidth(200)
        
        # Получаем список таймфреймов из метаданных или используем стандартный
        timeframes = meta.get("options", self.TIMEFRAMES)
        
        # Заполняем комбобокс
        for tf in timeframes:
            label = self.TIMEFRAME_LABELS.get(tf, tf)
            self.combo_box.addItem(label, tf)
        
        # Устанавливаем текущее значение
        if current_value is not None:
            index = self.combo_box.findData(current_value)
            if index >= 0:
                self.combo_box.setCurrentIndex(index)
        else:
            default = meta.get("default", "5m")
            index = self.combo_box.findData(default)
            if index >= 0:
                self.combo_box.setCurrentIndex(index)
        
        # Применяем tooltip
        if self.toolTip():
            self.combo_box.setToolTip(self.toolTip())
    
    def get_value(self) -> str:
        """Возвращает выбранный таймфрейм"""
        return self.combo_box.currentData()
    
    def set_value(self, value: Any):
        """Устанавливает таймфрейм"""
        index = self.combo_box.findData(value)
        if index >= 0:
            self.combo_box.setCurrentIndex(index)
    
    def validate(self) -> Tuple[bool, str]:
        """Валидация таймфрейма"""
        value = self.get_value()
        
        if not value:
            return False, "Необходимо выбрать таймфрейм"
        
        return True, ""


class CommissionParamWidget(BaseParamWidget):
    """Виджет для параметра commission с поддержкой режимов: Авто / Ручной (% / ₽)"""
    
    def __init__(self, key: str, meta: dict, current_value: Any, connector_id: str = None, parent=None):
        super().__init__(key, meta, current_value, connector_id, parent)
        
        # Чекбокс "Авто"
        self.chk_auto = QCheckBox("Авто", self)
        self.chk_auto.setToolTip("Использовать автоматический расчёт комиссий из настроек")
        self.chk_auto.toggled.connect(self._on_auto_toggled)
        
        # Создаём два спинбокса - для процентов и для рублей
        self.spin_pct = QDoubleSpinBox(self)
        self.spin_pct.setRange(0.0, 10.0)
        self.spin_pct.setDecimals(10)
        self.spin_pct.setSingleStep(0.01)
        self.spin_pct.setFixedWidth(120)
        self.spin_pct.setSuffix(" %")
        self.spin_pct.setToolTip("Комиссия в % от суммы сделки (для акций)")
        
        self.spin_rub = QDoubleSpinBox(self)
        self.spin_rub.setRange(0.0, 100_000.0)
        self.spin_rub.setDecimals(10)
        self.spin_rub.setSingleStep(0.1)
        self.spin_rub.setFixedWidth(120)
        self.spin_rub.setSuffix(" ₽")
        self.spin_rub.setToolTip("Комиссия в рублях за контракт (для фьючерсов)")
        
        # Layout для переключения виджетов
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.chk_auto)
        layout.addWidget(self.spin_pct)
        layout.addWidget(self.spin_rub)
        
        # Устанавливаем начальное значение
        # Если значение == "auto", включаем режим Авто
        if current_value == "auto":
            self.chk_auto.setChecked(True)
            self.spin_pct.setValue(0.0)
            self.spin_rub.setValue(0.0)
        elif current_value is not None:
            self.chk_auto.setChecked(False)
            try:
                val = float(current_value)
                self.spin_pct.setValue(val)
                self.spin_rub.setValue(val)
            except (ValueError, TypeError):
                default = meta.get("default", 0.0)
                self.spin_pct.setValue(float(default))
                self.spin_rub.setValue(float(default))
        else:
            self.chk_auto.setChecked(False)
            default = meta.get("default", 0.0)
            self.spin_pct.setValue(float(default))
            self.spin_rub.setValue(float(default))
        
        # По умолчанию показываем процентный виджет
        self._is_futures = False
        self._update_visibility()
    
    def _on_auto_toggled(self, checked: bool):
        """Обработчик переключения режима Авто"""
        self._update_visibility()
    
    def set_board_type(self, is_futures: bool):
        """Переключает виджет в зависимости от типа борды"""
        # Избегаем избыточных обновлений
        if self._is_futures == is_futures:
            return
        
        self._is_futures = is_futures
        self._update_visibility()
    
    def _update_visibility(self):
        """Обновляет видимость виджетов и форсирует перерисовку родителя"""
        auto_mode = self.chk_auto.isChecked()
        
        # В режиме Авто скрываем спинбоксы
        if auto_mode:
            self.spin_pct.setVisible(False)
            self.spin_rub.setVisible(False)
        else:
            # В ручном режиме показываем нужный спинбокс
            self.spin_pct.setVisible(not self._is_futures)
            self.spin_rub.setVisible(self._is_futures)
        
        self.updateGeometry()
        # Уведомляем родительский QFormLayout о смене размера
        parent = self.parentWidget()
        if parent is not None:
            parent.updateGeometry()
            parent.update()
    
    def get_value(self):
        """Возвращает текущее значение комиссии: "auto" или float"""
        if self.chk_auto.isChecked():
            return "auto"
        
        if self._is_futures:
            return self.spin_rub.value()
        else:
            return self.spin_pct.value()
    
    def set_value(self, value: Any):
        """Устанавливает значение комиссии"""
        if value == "auto":
            self.chk_auto.setChecked(True)
        else:
            self.chk_auto.setChecked(False)
            try:
                val = float(value)
                if self._is_futures:
                    self.spin_rub.setValue(val)
                else:
                    self.spin_pct.setValue(val)
            except (ValueError, TypeError):
                pass
    
    def validate(self) -> Tuple[bool, str]:
        """Валидация значения комиссии"""
        # В режиме Авто валидация не требуется
        if self.chk_auto.isChecked():
            return True, ""
        
        value = self.get_value()
        
        try:
            val = float(value)
            if val < 0:
                return False, "Комиссия не может быть отрицательной"
        except (ValueError, TypeError):
            return False, "Некорректное значение комиссии"
        
        return True, ""


class ParamWidgetFactory:
    """
    Фабрика для создания виджетов параметров стратегий.
    Использует реестр типов для маппинга type -> класс виджета.
    """
    
    _registry: dict[str, type[BaseParamWidget]] = {}
    
    @classmethod
    def register(cls, type_name: str, widget_class: type[BaseParamWidget]):
        """
        Регистрирует новый тип виджета в фабрике.
        
        Args:
            type_name: Название типа параметра (str, int, float, bool и т.д.)
            widget_class: Класс виджета, наследующий BaseParamWidget
        """
        cls._registry[type_name] = widget_class
    
    @classmethod
    def create(cls, key: str, meta: dict, current_value: Any, connector_id: str = None) -> BaseParamWidget:
        """
        Создаёт виджет параметра по типу из метаданных.
        
        Args:
            key: Ключ параметра
            meta: Метаданные параметра (type, min, max, description и т.д.)
            current_value: Текущее значение параметра
            connector_id: ID коннектора (для специфичных виджетов)
        
        Returns:
            Экземпляр виджета параметра
        """
        ptype = meta.get("type", "str")
        
        # Получаем класс виджета из реестра
        widget_class = cls._registry.get(ptype)
        
        # Fallback на StrParamWidget для неизвестных типов
        if widget_class is None:
            widget_class = StrParamWidget
        
        # Создаём и возвращаем виджет
        return widget_class(key, meta, current_value, connector_id)


# Регистрируем стандартные типы виджетов
ParamWidgetFactory.register("str", StrParamWidget)
ParamWidgetFactory.register("int", IntParamWidget)
ParamWidgetFactory.register("float", FloatParamWidget)
ParamWidgetFactory.register("bool", BoolParamWidget)
ParamWidgetFactory.register("time", TimeParamWidget)
ParamWidgetFactory.register("select", SelectParamWidget)
ParamWidgetFactory.register("choice", SelectParamWidget)
ParamWidgetFactory.register("ticker", TickerParamWidget)
ParamWidgetFactory.register("instruments", InstrumentsParamWidget)
ParamWidgetFactory.register("commission", CommissionParamWidget)
ParamWidgetFactory.register("timeframe", TimeframeParamWidget)
