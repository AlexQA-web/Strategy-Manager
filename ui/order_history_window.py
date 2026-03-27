from datetime import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from ui.icons import apply_icon
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QHeaderView,
)

from core.order_history import get_closed_order_pairs

STYLE_DIALOG = """
QDialog {
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: 'Segoe UI', Arial;
    font-size: 13px;
}
QTableWidget {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 6px;
    gridline-color: #313244;
    selection-background-color: #313244;
    selection-color: #cdd6f4;
}
QTableWidget::item {
    padding: 4px 8px;
}
QHeaderView::section {
    background-color: #11111b;
    color: #89b4fa;
    border: none;
    border-right: 1px solid #313244;
    padding: 6px;
    font-weight: bold;
}
QPushButton {
    background-color: #89b4fa;
    color: #1e1e2e;
    border: none;
    border-radius: 5px;
    padding: 7px 18px;
    font-weight: bold;
}
QPushButton:hover {
    background-color: #b4befe;
}
QLabel#title {
    color: #89b4fa;
    font-size: 16px;
    font-weight: bold;
}
QLabel#subtitle {
    color: #a6adc8;
    font-size: 12px;
}
"""


class OrderHistoryWindow(QDialog):
    COLUMNS = [
        ('Направление', 100),
        ('Тикер', 90),
        ('Дата открытия', 150),
        ('Дата закрытия', 150),
        ('Ср. цена входа', 110),
        ('Ср. цена выхода', 110),
        ('Кол-во лот', 90),
        ('Комиссия', 95),
        ('PnL', 95),
    ]

    def __init__(self, strategy_id: str, strategy_name: str, ticker: str | None = None, parent=None):
        super().__init__(parent)
        self._strategy_id = strategy_id
        self._strategy_name = strategy_name
        self._ticker = ticker
        self.setWindowTitle('История ордеров')
        self.setMinimumSize(900, 480)
        self.resize(1100, 620)
        self.setStyleSheet(STYLE_DIALOG)
        self._build_ui()
        self._load_data()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel('История ордеров')
        title.setObjectName('title')
        layout.addWidget(title)

        scope = self._strategy_name
        if self._ticker:
            scope += f' → {self._ticker}'
        subtitle = QLabel(scope)
        subtitle.setObjectName('subtitle')
        layout.addWidget(subtitle)

        self._table = QTableWidget()
        self._table.setColumnCount(len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels([name for name, _ in self.COLUMNS])
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionsMovable(True)
        header.setDragEnabled(True)
        header.setStretchLastSection(False)
        for idx, (_, width) in enumerate(self.COLUMNS):
            header.setSectionResizeMode(idx, QHeaderView.ResizeMode.Interactive)
            self._table.setColumnWidth(idx, width)
        layout.addWidget(self._table, stretch=1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        btn_close = QPushButton('Закрыть')
        apply_icon(btn_close, 'actions/close.svg', 14)
        btn_close.clicked.connect(self.accept)
        buttons.addWidget(btn_close)
        layout.addLayout(buttons)

    def _load_data(self):
        pairs = get_closed_order_pairs(self._strategy_id, ticker=self._ticker)
        self._table.setRowCount(len(pairs))

        for row, pair in enumerate(pairs):
            open_order = pair['open']
            close_order = pair['close']
            pnl = float(pair.get('pnl', 0.0) or 0.0)
            commission = float(pair.get('commission', 0.0) or 0.0)
            qty = int(pair.get('quantity') or open_order.get('quantity', 0) or 0)

            direction = 'BUY' if pair.get('is_long') else 'SELL'
            values = [
                direction,
                open_order.get('ticker', '—'),
                self._fmt_dt(open_order.get('timestamp')),
                self._fmt_dt(close_order.get('timestamp') if close_order else None),
                self._fmt_price(open_order.get('price')),
                self._fmt_price(close_order.get('price') if close_order else None),
                str(qty),
                f'{commission:.2f}',
                f'{pnl:+.2f}',
            ]

            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if col == 0:
                    item.setForeground(QColor('#a6e3a1' if direction == 'BUY' else '#f38ba8'))
                elif col == 8:
                    item.setForeground(QColor('#a6e3a1' if pnl >= 0 else '#f38ba8'))
                elif col == 7:
                    item.setForeground(QColor('#f9e2af'))
                self._table.setItem(row, col, item)

        self._table.sortItems(1, Qt.SortOrder.DescendingOrder)

    @staticmethod
    def _fmt_dt(value) -> str:
        if not value:
            return '—'
        try:
            return datetime.fromisoformat(str(value)).strftime('%d.%m.%Y %H:%M:%S')
        except Exception:
            return str(value)

    @staticmethod
    def _fmt_price(value) -> str:
        if value in (None, ''):
            return '—'
        try:
            return f'{float(value):.4f}'
        except (TypeError, ValueError):
            return str(value)
