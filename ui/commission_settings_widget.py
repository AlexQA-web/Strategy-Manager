"""
Виджет настроек комиссий.

Встраивается как вкладка в окно настроек для управления ставками комиссий.
"""

import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QDoubleSpinBox, QComboBox, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QMessageBox, QScrollArea
)
from PyQt6.QtCore import Qt


class _NoScrollSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox, который не реагирует на скролл пока не получит фокус кликом."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):
        if not self.hasFocus():
            event.ignore()
            return
        super().wheelEvent(event)

from core.commission_manager import commission_manager
from core.instrument_classifier import instrument_classifier
from core.moex_commission_fetcher import moex_commission_fetcher
from ui.commission_preview_dialog import CommissionPreviewDialog

logger = logging.getLogger(__name__)


class CommissionSettingsWidget(QWidget):
    """
    Виджет для настройки ставок комиссий.

    Позволяет редактировать:
    - Ставки MOEX (тейкер) для всех типов инструментов
    - Ставки брокера (фьючерсы в рублях, акции в процентах)
    - Правила классификации инструментов
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()
        self._load_settings()
        self._check_rates_freshness()
    
    def _build_ui(self):
        """Создаёт интерфейс виджета."""
        # Создаём основной layout для виджета
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        # Создаём scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        
        # Создаём контейнер для содержимого
        content_widget = QWidget()
        layout = QVBoxLayout(content_widget)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)
        
        # Секция: Ставки MOEX
        moex_group = self._create_moex_section()
        layout.addWidget(moex_group)
        
        # Секция: Ставки брокера Транзак
        broker_transaq_group = self._create_broker_section()
        layout.addWidget(broker_transaq_group)
        
        # Секция: Ставки брокера КВИК
        broker_quik_group = self._create_broker_quik_section()
        layout.addWidget(broker_quik_group)
        
        # Секция: Классификация инструментов
        classifier_group = self._create_classifier_section()
        layout.addWidget(classifier_group)
        
        # Кнопки действий
        buttons_layout = QHBoxLayout()
        
        preview_btn = QPushButton("🧮 Предпросмотр расчёта")
        preview_btn.clicked.connect(self._show_preview)
        buttons_layout.addWidget(preview_btn)
        
        buttons_layout.addStretch()
        
        save_btn = QPushButton("💾 Сохранить")
        save_btn.setObjectName("btn_save")
        save_btn.clicked.connect(self._save_settings)
        buttons_layout.addWidget(save_btn)
        
        layout.addLayout(buttons_layout)
        layout.addStretch()
        
        # Устанавливаем контейнер в scroll area
        scroll.setWidget(content_widget)
        
        # Добавляем scroll area в основной layout
        main_layout.addWidget(scroll)
    
    def _create_moex_section(self) -> QGroupBox:
        """Создаёт секцию настроек ставок MOEX."""
        group = QGroupBox("Ставки MOEX (Тейкер, %)")
        form = QFormLayout(group)
        form.setSpacing(12)
        
        # Создаём спинбоксы для каждого типа инструмента
        self.moex_spinboxes = {}
        
        types_labels = {
            "currency_futures": "Валютные фьючерсы:",
            "equity_futures": "Фондовые фьючерсы:",
            "index_futures": "Индексные фьючерсы:",
            "commodity_futures": "Товарные фьючерсы:",
            "stock": "Акции:",
            "bond": "Облигации:",
            "etf": "ETF:"
        }
        
        for inst_type, label in types_labels.items():
            spinbox = _NoScrollSpinBox()
            spinbox.setRange(0.0, 1.0)
            spinbox.setDecimals(5)
            spinbox.setSingleStep(0.00001)
            spinbox.setSuffix(" %")
            self.moex_spinboxes[inst_type] = spinbox
            form.addRow(label, spinbox)
        
        # Подпись
        note = QLabel("Ставки MOEX для мейкера всегда 0%")
        note.setStyleSheet("color: gray; font-size: 11px;")
        form.addRow("", note)
        
        # Кнопка обновления с MOEX
        update_btn = QPushButton("🔄 Обновить с сайта MOEX")
        update_btn.clicked.connect(self._update_from_moex)
        form.addRow("", update_btn)
        
        # Дата последнего обновления
        self.last_update_label = QLabel()
        self.last_update_label.setStyleSheet("color: gray; font-size: 11px;")
        form.addRow("Последнее обновление:", self.last_update_label)
        
        return group
    
    def _create_broker_section(self) -> QGroupBox:
        """Создаёт секцию настроек ставок брокера Транзак."""
        group = QGroupBox("Ставки брокера Транзак")
        layout = QVBoxLayout(group)
        layout.setSpacing(16)
        
        # Подсекция: Фьючерсы
        futures_group = QGroupBox("Фьючерсы (рублей за контракт, одна сторона)")
        futures_form = QFormLayout(futures_group)
        futures_form.setSpacing(12)
        
        self.broker_transaq_futures_spinboxes = {}
        
        futures_labels = {
            "currency_futures": "Валютные:",
            "equity_futures": "Фондовые:",
            "index_futures": "Индексные:",
            "commodity_futures": "Товарные:"
        }
        
        for inst_type, label in futures_labels.items():
            spinbox = _NoScrollSpinBox()
            spinbox.setRange(0.0, 100.0)
            spinbox.setDecimals(2)
            spinbox.setSingleStep(0.01)
            spinbox.setSuffix(" ₽/контр.")
            self.broker_transaq_futures_spinboxes[inst_type] = spinbox
            futures_form.addRow(label, spinbox)
        
        note = QLabel("Фиксированная сумма в рублях за каждый открытый/закрытый контракт")
        note.setStyleSheet("color: gray; font-size: 11px;")
        futures_form.addRow("", note)
        
        layout.addWidget(futures_group)
        
        # Подсекция: Акции/Облигации/ETF
        stock_group = QGroupBox("Акции / Облигации / ETF (% от суммы)")
        stock_form = QFormLayout(stock_group)
        stock_form.setSpacing(12)
        
        self.broker_transaq_stock_spinboxes = {}
        
        stock_labels = {
            "stock_pct": "Акции:",
            "bond_pct": "Облигации:",
            "etf_pct": "ETF:"
        }
        
        for key, label in stock_labels.items():
            spinbox = _NoScrollSpinBox()
            spinbox.setRange(0.0, 1.0)
            spinbox.setDecimals(4)
            spinbox.setSingleStep(0.0001)
            spinbox.setSuffix(" %")
            self.broker_transaq_stock_spinboxes[key] = spinbox
            stock_form.addRow(label, spinbox)
        
        note = QLabel("Процент от суммы сделки")
        note.setStyleSheet("color: gray; font-size: 11px;")
        stock_form.addRow("", note)
        
        layout.addWidget(stock_group)
        
        return group
    
    def _create_broker_quik_section(self) -> QGroupBox:
        """Создаёт секцию настроек ставок брокера КВИК."""
        group = QGroupBox("Ставки брокера КВИК")
        layout = QVBoxLayout(group)
        layout.setSpacing(16)
        
        # Подсекция: Фьючерсы
        futures_group = QGroupBox("Фьючерсы (рублей за контракт, одна сторона)")
        futures_form = QFormLayout(futures_group)
        futures_form.setSpacing(12)
        
        self.broker_quik_futures_spinboxes = {}
        
        futures_labels = {
            "currency_futures": "Валютные:",
            "equity_futures": "Фондовые:",
            "index_futures": "Индексные:",
            "commodity_futures": "Товарные:"
        }
        
        for inst_type, label in futures_labels.items():
            spinbox = _NoScrollSpinBox()
            spinbox.setRange(0.0, 100.0)
            spinbox.setDecimals(2)
            spinbox.setSingleStep(0.01)
            spinbox.setSuffix(" ₽/контр.")
            self.broker_quik_futures_spinboxes[inst_type] = spinbox
            futures_form.addRow(label, spinbox)
        
        note = QLabel("Фиксированная сумма в рублях за каждый открытый/закрытый контракт")
        note.setStyleSheet("color: gray; font-size: 11px;")
        futures_form.addRow("", note)
        
        layout.addWidget(futures_group)
        
        # Подсекция: Акции/Облигации/ETF
        stock_group = QGroupBox("Акции / Облигации / ETF (% от суммы)")
        stock_form = QFormLayout(stock_group)
        stock_form.setSpacing(12)
        
        self.broker_quik_stock_spinboxes = {}
        
        stock_labels = {
            "stock_pct": "Акции:",
            "bond_pct": "Облигации:",
            "etf_pct": "ETF:"
        }
        
        for key, label in stock_labels.items():
            spinbox = _NoScrollSpinBox()
            spinbox.setRange(0.0, 1.0)
            spinbox.setDecimals(4)
            spinbox.setSingleStep(0.0001)
            spinbox.setSuffix(" %")
            self.broker_quik_stock_spinboxes[key] = spinbox
            stock_form.addRow(label, spinbox)
        
        note = QLabel("Процент от суммы сделки")
        note.setStyleSheet("color: gray; font-size: 11px;")
        stock_form.addRow("", note)
        
        layout.addWidget(stock_group)
        
        return group
    
    def _create_classifier_section(self) -> QGroupBox:
        """Создаёт секцию настроек классификации инструментов."""
        group = QGroupBox("Классификация инструментов")
        layout = QVBoxLayout(group)
        layout.setSpacing(16)
        
        # Подсекция: Правила по префиксам
        prefix_group = QGroupBox("Правила по префиксам")
        prefix_layout = QVBoxLayout(prefix_group)
        
        self.prefix_table = QTableWidget()
        self.prefix_table.setColumnCount(2)
        self.prefix_table.setHorizontalHeaderLabels(["Префикс", "Тип инструмента"])
        self.prefix_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.prefix_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.prefix_table.setAlternatingRowColors(True)
        prefix_layout.addWidget(self.prefix_table)
        
        prefix_buttons = QHBoxLayout()
        add_prefix_btn = QPushButton("➕ Добавить")
        add_prefix_btn.clicked.connect(lambda: self._add_table_row(self.prefix_table))
        prefix_buttons.addWidget(add_prefix_btn)
        
        del_prefix_btn = QPushButton("➖ Удалить выбранные")
        del_prefix_btn.clicked.connect(lambda: self._delete_selected_rows(self.prefix_table))
        prefix_buttons.addWidget(del_prefix_btn)
        prefix_buttons.addStretch()
        
        prefix_layout.addLayout(prefix_buttons)
        layout.addWidget(prefix_group)
        
        # Подсекция: Ручной маппинг
        manual_group = QGroupBox("Ручной маппинг тикеров")
        manual_layout = QVBoxLayout(manual_group)
        
        self.manual_table = QTableWidget()
        self.manual_table.setColumnCount(2)
        self.manual_table.setHorizontalHeaderLabels(["Тикер", "Тип инструмента"])
        self.manual_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.manual_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.manual_table.setAlternatingRowColors(True)
        manual_layout.addWidget(self.manual_table)
        
        manual_buttons = QHBoxLayout()
        add_manual_btn = QPushButton("➕ Добавить")
        add_manual_btn.clicked.connect(lambda: self._add_table_row(self.manual_table))
        manual_buttons.addWidget(add_manual_btn)
        
        del_manual_btn = QPushButton("➖ Удалить выбранные")
        del_manual_btn.clicked.connect(lambda: self._delete_selected_rows(self.manual_table))
        manual_buttons.addWidget(del_manual_btn)
        manual_buttons.addStretch()
        
        manual_layout.addLayout(manual_buttons)
        layout.addWidget(manual_group)
        
        return group
    
    def _add_table_row(self, table: QTableWidget):
        """Добавляет новую строку в таблицу."""
        row = table.rowCount()
        table.insertRow(row)
        
        # Добавляем комбобокс с типами инструментов во вторую колонку
        combo = QComboBox()
        combo.addItems([
            "currency_futures",
            "equity_futures",
            "index_futures",
            "commodity_futures",
            "stock",
            "bond",
            "etf"
        ])
        table.setCellWidget(row, 1, combo)
    
    def _delete_selected_rows(self, table: QTableWidget):
        """Удаляет выбранные строки из таблицы."""
        selected_rows = set(index.row() for index in table.selectedIndexes())
        for row in sorted(selected_rows, reverse=True):
            table.removeRow(row)
    
    def _load_settings(self):
        """Загружает текущие настройки."""
        config = commission_manager.config
        
        # Загружаем ставки MOEX
        moex_taker = config.get("moex", {}).get("taker_pct", {})
        for inst_type, spinbox in self.moex_spinboxes.items():
            spinbox.setValue(moex_taker.get(inst_type, 0.0))
        
        # Загружаем ставки брокера Транзак для фьючерсов
        broker_transaq_futures = config.get("broker_transaq", {}).get("futures_rub", {})
        for inst_type, spinbox in self.broker_transaq_futures_spinboxes.items():
            spinbox.setValue(broker_transaq_futures.get(inst_type, 0.0))
        
        # Загружаем ставки брокера Транзак для акций
        broker_transaq_config = config.get("broker_transaq", {})
        for key, spinbox in self.broker_transaq_stock_spinboxes.items():
            spinbox.setValue(broker_transaq_config.get(key, 0.0))
        
        # Загружаем ставки брокера КВИК для фьючерсов
        broker_quik_futures = config.get("broker_quik", {}).get("futures_rub", {})
        for inst_type, spinbox in self.broker_quik_futures_spinboxes.items():
            spinbox.setValue(broker_quik_futures.get(inst_type, 0.0))
        
        # Загружаем ставки брокера КВИК для акций
        broker_quik_config = config.get("broker_quik", {})
        for key, spinbox in self.broker_quik_stock_spinboxes.items():
            spinbox.setValue(broker_quik_config.get(key, 0.0))
        
        # Загружаем правила по префиксам
        prefix_rules = instrument_classifier.prefix_rules
        self.prefix_table.setRowCount(len(prefix_rules))
        for i, (prefix, inst_type) in enumerate(prefix_rules.items()):
            self.prefix_table.setItem(i, 0, QTableWidgetItem(prefix))
            combo = QComboBox()
            combo.addItems([
                "currency_futures", "equity_futures", "index_futures", "commodity_futures",
                "stock", "bond", "etf"
            ])
            combo.setCurrentText(inst_type)
            self.prefix_table.setCellWidget(i, 1, combo)
        
        # Загружаем ручной маппинг
        manual_mapping = instrument_classifier.manual_mapping
        self.manual_table.setRowCount(len(manual_mapping))
        for i, (ticker, inst_type) in enumerate(manual_mapping.items()):
            self.manual_table.setItem(i, 0, QTableWidgetItem(ticker))
            combo = QComboBox()
            combo.addItems([
                "currency_futures", "equity_futures", "index_futures", "commodity_futures",
                "stock", "bond", "etf"
            ])
            combo.setCurrentText(inst_type)
            self.manual_table.setCellWidget(i, 1, combo)
        
        # Обновляем дату последнего обновления
        last_update = config.get("last_moex_update", "Неизвестно")
        days = commission_manager.days_since_update()
        if days is not None:
            self.last_update_label.setText(f"{last_update} ({days} дн. назад)")
        else:
            self.last_update_label.setText(last_update)
    
    def _save_settings(self):
        """Сохраняет настройки."""
        try:
            config = commission_manager.config
            
            # Сохраняем ставки MOEX
            if "moex" not in config:
                config["moex"] = {"taker_pct": {}, "maker_pct": {}}
            
            for inst_type, spinbox in self.moex_spinboxes.items():
                config["moex"]["taker_pct"][inst_type] = spinbox.value()
            
            # Сохраняем ставки брокера Транзак для фьючерсов
            if "broker_transaq" not in config:
                config["broker_transaq"] = {"futures_rub": {}}
            
            for inst_type, spinbox in self.broker_transaq_futures_spinboxes.items():
                config["broker_transaq"]["futures_rub"][inst_type] = spinbox.value()
            
            # Сохраняем ставки брокера Транзак для акций
            for key, spinbox in self.broker_transaq_stock_spinboxes.items():
                config["broker_transaq"][key] = spinbox.value()
            
            # Сохраняем ставки брокера КВИК для фьючерсов
            if "broker_quik" not in config:
                config["broker_quik"] = {"futures_rub": {}}
            
            for inst_type, spinbox in self.broker_quik_futures_spinboxes.items():
                config["broker_quik"]["futures_rub"][inst_type] = spinbox.value()
            
            # Сохраняем ставки брокера КВИК для акций
            for key, spinbox in self.broker_quik_stock_spinboxes.items():
                config["broker_quik"][key] = spinbox.value()
            
            # Сохраняем правила по префиксам
            prefix_rules = {}
            for row in range(self.prefix_table.rowCount()):
                prefix_item = self.prefix_table.item(row, 0)
                combo = self.prefix_table.cellWidget(row, 1)
                if prefix_item and combo:
                    prefix = prefix_item.text().strip()
                    if prefix:
                        prefix_rules[prefix] = combo.currentText()
            
            instrument_classifier.prefix_rules = prefix_rules
            
            # Сохраняем ручной маппинг
            manual_mapping = {}
            for row in range(self.manual_table.rowCount()):
                ticker_item = self.manual_table.item(row, 0)
                combo = self.manual_table.cellWidget(row, 1)
                if ticker_item and combo:
                    ticker = ticker_item.text().strip().upper()
                    if ticker:
                        manual_mapping[ticker] = combo.currentText()
            
            instrument_classifier.manual_mapping = manual_mapping
            
            # Сохраняем конфиги
            commission_manager.save_config()
            instrument_classifier.save_config()
            
            QMessageBox.information(self, "Успех", "Настройки комиссий сохранены")
            logger.info("[CommissionSettings] Настройки сохранены")
        
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить настройки: {e}")
            logger.error(f"[CommissionSettings] Ошибка сохранения: {e}")
    
    def _update_from_moex(self):
        """Обновляет ставки с сайта MOEX."""
        QMessageBox.information(
            self,
            "В разработке",
            "Автоматическое обновление ставок с сайта MOEX будет реализовано в следующей версии.\n\n"
            "Пока обновляйте ставки вручную."
        )
    
    def _show_preview(self):
        """Показывает диалог предпросмотра расчёта."""
        dialog = CommissionPreviewDialog(parent=self)
        dialog.exec()
    
    def _check_rates_freshness(self):
        """Проверяет актуальность ставок MOEX и показывает предупреждение при необходимости."""
        age = moex_commission_fetcher.get_cache_age()
        
        if age is None:
            # Кэш отсутствует - первый запуск
            logger.info("[CommissionSettings] Кэш ставок MOEX отсутствует (первый запуск)")
            return
        
        if moex_commission_fetcher.is_cache_outdated():
            days = age.days
            hours = age.seconds // 3600
            
            msg = (
                f"⚠️ Ставки комиссий MOEX могли устареть\n\n"
                f"Последнее обновление: {days} дн. {hours} ч. назад\n\n"
                f"Рекомендуется проверить актуальность ставок на сайте MOEX:\n"
                f"https://www.moex.com/ru/tariffs/\n\n"
                f"При необходимости обновите ставки вручную в настройках."
            )
            
            QMessageBox.warning(self, "Устаревшие ставки", msg)
            logger.warning(f"[CommissionSettings] Ставки MOEX устарели (возраст: {age})")
