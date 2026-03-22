# ui/instruments_editor.py
"""
Редактор корзины инструментов для стратегии Achilles.
Каждая строка: TickerSelector + чекбоксы Покупка/Продажа + выбор типа заявки.
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QScrollArea, QWidget,
    QPushButton, QCheckBox, QComboBox, QLabel, QFrame,
)
from PyQt6.QtCore import Qt
from loguru import logger


class _InstrumentRow(QWidget):
    """Одна строка: TickerSelector + Покупка + Продажа + Тип заявки + Удалить."""

    def __init__(self, connector_id: str, instr: dict, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(8)

        from ui.ticker_selector import TickerSelector
        self._ticker_sel = TickerSelector(
            connector_id=connector_id,
            current_ticker=instr.get("ticker", ""),
            current_board=instr.get("board", "TQBR"),
        )
        layout.addWidget(self._ticker_sel, stretch=1)

        self._chk_buy = QCheckBox("Покупка")
        self._chk_buy.setChecked(instr.get("allow_buy", True))
        layout.addWidget(self._chk_buy)

        self._chk_sell = QCheckBox("Продажа")
        self._chk_sell.setChecked(instr.get("allow_sell", True))
        layout.addWidget(self._chk_sell)

        self._cmb_mode = QComboBox()
        self._cmb_mode.addItem("Лимитка (Стакан)", "limit_book")
        self._cmb_mode.addItem("Лимитка (Цена)",   "limit_price")
        mode = instr.get("order_mode", "limit_book")
        idx = self._cmb_mode.findData(mode)
        if idx >= 0:
            self._cmb_mode.setCurrentIndex(idx)
        self._cmb_mode.setFixedWidth(150)
        layout.addWidget(self._cmb_mode)

        self._btn_del = QPushButton("✕")
        self._btn_del.setFixedSize(28, 28)
        self._btn_del.setStyleSheet("color: #f38ba8; font-weight: bold;")
        self._btn_del.clicked.connect(self._remove)
        layout.addWidget(self._btn_del)

    def _remove(self):
        parent = self.parent()
        self.setParent(None)
        self.deleteLater()
        # Обновляем layout родителя
        if parent and parent.layout():
            parent.layout().update()

    def get_value(self) -> dict:
        return {
            "ticker":     self._ticker_sel.ticker(),
            "board":      self._ticker_sel.board(),
            "allow_buy":  self._chk_buy.isChecked(),
            "allow_sell": self._chk_sell.isChecked(),
            "order_mode": self._cmb_mode.currentData(),
        }


class InstrumentsEditor(QDialog):
    """Диалог редактирования корзины инструментов."""

    def __init__(self, connector_id: str, instruments: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Корзина инструментов")
        self.setMinimumSize(780, 480)
        self.resize(860, 560)
        self._connector_id = connector_id
        self._result: list | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Заголовок колонок
        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 32, 0)
        for text, stretch, width in [
            ("Борд / Тикер", 1, None),
            ("Покупка", 0, 80),
            ("Продажа", 0, 80),
            ("Тип заявки", 0, 150),
        ]:
            lbl = QLabel(text)
            lbl.setStyleSheet("color: #89b4fa; font-weight: bold; font-size: 12px;")
            if width:
                lbl.setFixedWidth(width)
            hdr.addWidget(lbl, stretch=stretch)
        layout.addLayout(hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #313244;")
        layout.addWidget(sep)

        # Скролл-область со строками
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._rows_widget = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_widget)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(2)
        self._rows_layout.addStretch()
        self._scroll.setWidget(self._rows_widget)
        layout.addWidget(self._scroll, stretch=1)

        # Загружаем инструменты
        for instr in instruments:
            self._add_row(instr)

        # Кнопка добавить
        btn_add = QPushButton("+ Добавить инструмент")
        btn_add.setObjectName("btn_add")
        btn_add.clicked.connect(lambda: self._add_row({}))
        layout.addWidget(btn_add, alignment=Qt.AlignmentFlag.AlignLeft)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("color: #313244;")
        layout.addWidget(sep2)

        # Кнопки OK / Отмена
        btn_row = QHBoxLayout()
        btn_cancel = QPushButton("Отмена")
        btn_cancel.setFixedWidth(90)
        btn_cancel.clicked.connect(self.reject)
        btn_ok = QPushButton("💾 Сохранить")
        btn_ok.setObjectName("btn_save")
        btn_ok.setFixedWidth(130)
        btn_ok.clicked.connect(self._save)
        btn_row.addWidget(btn_cancel)
        btn_row.addStretch()
        btn_row.addWidget(btn_ok)
        layout.addLayout(btn_row)

    def _add_row(self, instr: dict):
        row = _InstrumentRow(self._connector_id, instr, self._rows_widget)
        # Вставляем перед stretch
        count = self._rows_layout.count()
        self._rows_layout.insertWidget(count - 1, row)

    def _save(self):
        result = []
        for i in range(self._rows_layout.count()):
            item = self._rows_layout.itemAt(i)
            if item and isinstance(item.widget(), _InstrumentRow):
                val = item.widget().get_value()
                if val["ticker"]:
                    result.append(val)
        self._result = result
        self.accept()

    def get_result(self) -> list | None:
        return self._result
