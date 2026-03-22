"""
Диалог предпросмотра расчёта комиссии.

Позволяет пользователю ввести параметры сделки и увидеть детализированный расчёт комиссии.
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPushButton,
    QTableWidget, QTableWidgetItem, QGroupBox
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from core.commission_manager import commission_manager
from core.instrument_classifier import instrument_classifier


class CommissionPreviewDialog(QDialog):
    """
    Диалог для предпросмотра расчёта комиссии.
    
    Показывает детализированный расчёт комиссии для заданных параметров сделки.
    """
    
    def __init__(self, ticker: str = "", board: str = "TQBR", parent=None):
        """
        Инициализация диалога.
        
        Args:
            ticker: Начальный тикер
            board: Начальная борда
            parent: Родительский виджет
        """
        super().__init__(parent)
        self.setWindowTitle("🧮 Предпросмотр расчёта комиссии")
        self.setMinimumWidth(600)
        self.setMinimumHeight(500)
        
        self._build_ui()
        
        # Устанавливаем начальные значения
        if ticker:
            self.ticker_input.setText(ticker)
        if board:
            self.board_input.setCurrentText(board)
        
        # Автоматически рассчитываем при открытии
        if ticker:
            self._calculate()
    
    def _build_ui(self):
        """Создаёт интерфейс диалога."""
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)
        
        # Группа параметров
        params_group = QGroupBox("Параметры сделки")
        params_layout = QFormLayout(params_group)
        params_layout.setSpacing(12)
        
        # Тикер
        self.ticker_input = QLineEdit()
        self.ticker_input.setPlaceholderText("Например: Si, SBER, BR")
        params_layout.addRow("Тикер:", self.ticker_input)
        
        # Борда
        self.board_input = QComboBox()
        self.board_input.addItems(["TQBR", "SPBFUT", "TQTF", "TQOB"])
        self.board_input.setEditable(True)
        params_layout.addRow("Борда:", self.board_input)
        
        # Роль ордера
        self.role_input = QComboBox()
        self.role_input.addItem("Тейкер (рыночные и лимитные в стакан)", "taker")
        self.role_input.addItem("Мейкер (лимитные в очередь)", "maker")
        params_layout.addRow("Роль ордера:", self.role_input)
        
        # Количество
        self.quantity_input = QSpinBox()
        self.quantity_input.setRange(1, 1000000)
        self.quantity_input.setValue(1)
        self.quantity_input.setSuffix(" шт.")
        params_layout.addRow("Количество:", self.quantity_input)
        
        # Цена
        self.price_input = QDoubleSpinBox()
        self.price_input.setRange(0.01, 1000000.0)
        self.price_input.setValue(100.0)
        self.price_input.setDecimals(2)
        self.price_input.setSuffix(" ₽")
        params_layout.addRow("Цена:", self.price_input)
        
        # Стоимость пункта (для фьючерсов)
        self.point_cost_input = QDoubleSpinBox()
        self.point_cost_input.setRange(0.01, 10000.0)
        self.point_cost_input.setValue(1.0)
        self.point_cost_input.setDecimals(2)
        self.point_cost_input.setSuffix(" ₽")
        self.point_cost_label = QLabel("Стоимость пункта:")
        params_layout.addRow(self.point_cost_label, self.point_cost_input)
        
        layout.addWidget(params_group)
        
        # Кнопка расчёта
        calc_btn = QPushButton("🧮 Рассчитать")
        calc_btn.setObjectName("btn_calculate")
        calc_btn.clicked.connect(self._calculate)
        layout.addWidget(calc_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        
        # Таблица результатов
        results_group = QGroupBox("Результат расчёта")
        results_layout = QVBoxLayout(results_group)
        
        self.results_table = QTableWidget()
        self.results_table.setColumnCount(2)
        self.results_table.setHorizontalHeaderLabels(["Параметр", "Значение"])
        self.results_table.horizontalHeader().setStretchLastSection(True)
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.setAlternatingRowColors(True)
        
        results_layout.addWidget(self.results_table)
        layout.addWidget(results_group)
        
        # Кнопка закрытия
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)
        
        # Подключаем автообновление при изменении тикера/борды
        self.ticker_input.textChanged.connect(self._on_ticker_changed)
        self.board_input.currentTextChanged.connect(self._on_ticker_changed)
    
    def _on_ticker_changed(self):
        """Обработчик изменения тикера/борды - показывает/скрывает поле стоимости пункта."""
        ticker = self.ticker_input.text().strip()
        board = self.board_input.currentText()
        
        if not ticker:
            return
        
        # Проверяем, является ли инструмент фьючерсом
        is_futures = instrument_classifier.is_futures(ticker, board)
        
        # Показываем/скрываем поле стоимости пункта
        self.point_cost_label.setVisible(is_futures)
        self.point_cost_input.setVisible(is_futures)
    
    def _calculate(self):
        """Выполняет расчёт комиссии и отображает результат."""
        # Получаем параметры
        ticker = self.ticker_input.text().strip()
        board = self.board_input.currentText()
        quantity = self.quantity_input.value()
        price = self.price_input.value()
        order_role = self.role_input.currentData()
        point_cost = self.point_cost_input.value()
        
        if not ticker:
            self._show_error("Укажите тикер")
            return
        
        # Проверяем тип инструмента
        is_futures = instrument_classifier.is_futures(ticker, board)
        
        # Обновляем видимость поля стоимости пункта
        self.point_cost_label.setVisible(is_futures)
        self.point_cost_input.setVisible(is_futures)
        
        # Получаем детализацию расчёта
        breakdown = commission_manager.get_breakdown(
            ticker=ticker,
            board=board,
            quantity=quantity,
            price=price,
            order_role=order_role,
            point_cost=point_cost if is_futures else None,
            connector_id="transaq"  # Используем коннектор по умолчанию для предварительного просмотра
        )
        
        # Отображаем результат
        self._display_breakdown(breakdown)
    
    def _display_breakdown(self, breakdown: dict):
        """
        Отображает детализацию расчёта в таблице.
        
        Args:
            breakdown: Словарь с детализацией расчёта
        """
        # Очищаем таблицу
        self.results_table.setRowCount(0)
        
        # Определяем строки для отображения
        rows = [
            ("Тип инструмента", self._format_instrument_type(breakdown["instrument_type"])),
            ("Сумма сделки", f"{breakdown['trade_value']:.2f} ₽"),
            ("Ставка MOEX ({})".format(breakdown["order_role"]), f"{breakdown['moex_pct']:.4f}%"),
            ("Комиссия MOEX", f"{breakdown['moex_rub']:.2f} ₽"),
        ]
        
        # Добавляем брокерскую комиссию
        if breakdown["is_futures"]:
            rows.append(("Ставка брокера", f"{breakdown['broker_rub']:.2f} ₽/контр."))
            rows.append(("Комиссия брокера", f"{breakdown['broker_rub'] * self.quantity_input.value():.2f} ₽"))
        else:
            rows.append(("Ставка брокера", f"{breakdown['broker_pct']:.4f}%"))
            rows.append(("Комиссия брокера", f"{breakdown['broker_rub']:.2f} ₽"))
        
        rows.extend([
            ("", ""),  # Разделитель
            ("Итого (1 сторона)", f"{breakdown['total_one_side']:.2f} ₽"),
            ("Итого (сделка)", f"{breakdown['total_roundtrip']:.2f} ₽"),
        ])
        
        # Заполняем таблицу
        self.results_table.setRowCount(len(rows))
        
        for i, (param, value) in enumerate(rows):
            # Параметр
            param_item = QTableWidgetItem(param)
            if i >= len(rows) - 2:  # Итоговые строки
                font = QFont()
                font.setBold(True)
                param_item.setFont(font)
            self.results_table.setItem(i, 0, param_item)
            
            # Значение
            value_item = QTableWidgetItem(value)
            value_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if i >= len(rows) - 2:  # Итоговые строки
                font = QFont()
                font.setBold(True)
                value_item.setFont(font)
            self.results_table.setItem(i, 1, value_item)
        
        # Растягиваем первую колонку
        self.results_table.resizeColumnToContents(0)
    
    def _format_instrument_type(self, instrument_type: str) -> str:
        """Форматирует тип инструмента для отображения."""
        type_names = {
            "currency_futures": "Валютный фьючерс",
            "equity_futures": "Фондовый фьючерс",
            "index_futures": "Индексный фьючерс",
            "commodity_futures": "Товарный фьючерс",
            "stock": "Акция",
            "bond": "Облигация",
            "etf": "ETF"
        }
        return type_names.get(instrument_type, instrument_type)
    
    def _show_error(self, message: str):
        """Отображает ошибку в таблице результатов."""
        self.results_table.setRowCount(1)
        error_item = QTableWidgetItem(f"⚠ {message}")
        error_item.setForeground(Qt.GlobalColor.red)
        self.results_table.setItem(0, 0, error_item)
