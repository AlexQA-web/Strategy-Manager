"""
Виджет выбора корзины инструментов.
Вынесен из strategy_window.py для устранения циклического импорта с param_widgets.py.
"""
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QLabel, QPushButton

from loguru import logger


class InstrumentsWidget(QWidget):
    """Кнопка открытия редактора корзины инструментов."""

    def __init__(self, connector_id: str, instruments: list, parent=None):
        super().__init__(parent)
        self._connector_id = connector_id
        self._instruments = list(instruments)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._lbl = QLabel(self._summary())
        self._lbl.setStyleSheet("color: #6c7086; font-size: 12px;")
        layout.addWidget(self._lbl, stretch=1)

        btn = QPushButton("\u270f Редактировать")
        btn.setFixedWidth(140)
        btn.clicked.connect(self._open_editor)
        layout.addWidget(btn)

    def _summary(self) -> str:
        n = len(self._instruments)
        return f"{n} инструмент(ов)" if n else "Нет инструментов"

    def _open_editor(self):
        from ui.instruments_editor import InstrumentsEditor
        dlg = InstrumentsEditor(self._connector_id, self._instruments, self)
        if dlg.exec() and dlg.get_result() is not None:
            self._instruments = dlg.get_result()
            self._lbl.setText(self._summary())

    def get_value(self) -> list:
        return self._instruments
