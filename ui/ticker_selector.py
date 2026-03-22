import threading
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QComboBox, QPushButton, QLabel
from PyQt6.QtCore import Qt, pyqtSignal, QObject
from loguru import logger


class _Worker(QObject):
    """Сигналы для передачи данных из фонового потока в UI."""
    securities_ready = pyqtSignal(list)
    boards_ready     = pyqtSignal(list)
    error            = pyqtSignal(str)


class TickerSelector(QWidget):
    """
    Умный виджет выбора тикера.
    - Подключён → ComboBox с поиском + кнопка 🔄
    - QUIK → дополнительный ComboBox выбора класса (борда)
    - Не подключён → поле ввода вручную
    """
    ticker_changed = pyqtSignal(str)
    board_changed = pyqtSignal(str)
    def __init__(self, connector_id: str = "finam",
                 current_ticker: str = "",
                 current_board:  str = "TQBR",
                 parent=None):
        super().__init__(parent)
        self._connector_id   = connector_id
        self._current_board  = current_board or "TQBR"

        # Worker с сигналами — он живёт в главном потоке
        self._worker = _Worker()
        self._worker.securities_ready.connect(self._update_combo)
        self._worker.boards_ready.connect(self._update_boards)
        self._worker.error.connect(self._on_error)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Выбор борда/класса
        self._cmb_board = QComboBox()
        self._cmb_board.setFixedWidth(100)
        self._cmb_board.setToolTip("Борд / класс бумаги")
        if connector_id == "finam":
            for b in ["TQBR", "FUT", "TQCB", "TQOB", "CETS", "SPBFUT"]:
                self._cmb_board.addItem(b)
        else:
            self._cmb_board.addItem(self._current_board)
        # Восстанавливаем сохранённый борд
        idx = self._cmb_board.findText(self._current_board)
        if idx >= 0:
            self._cmb_board.setCurrentIndex(idx)
        self._cmb_board.currentTextChanged.connect(self._on_board_changed)
        layout.addWidget(self._cmb_board)

        # ComboBox тикера
        self._cmb = QComboBox()
        self._cmb.setEditable(True)
        self._cmb.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._cmb.lineEdit().setPlaceholderText("Тикер...")
        self._cmb.setMinimumWidth(160)
        if current_ticker:
            self._cmb.addItem(current_ticker, current_ticker)
            self._cmb.setCurrentText(current_ticker)
        layout.addWidget(self._cmb, stretch=1)
        self._cmb.currentIndexChanged.connect(
            lambda: self.ticker_changed.emit(self.ticker())
        )

        # Кнопка обновить
        self._btn_refresh = QPushButton("🔄")
        self._btn_refresh.setFixedSize(30, 28)
        self._btn_refresh.setToolTip("Обновить список тикеров")
        self._btn_refresh.clicked.connect(self._load_securities)
        layout.addWidget(self._btn_refresh)

        # Статус
        self._lbl = QLabel("")
        self._lbl.setStyleSheet("color: #6c7086; font-size: 11px;")
        layout.addWidget(self._lbl)

        self._load_securities()

    # ─────────────────────────────────────────────
    # Загрузка данных
    # ─────────────────────────────────────────────

    def _get_connector(self):
        from core.connector_manager import connector_manager
        return connector_manager.get(self._connector_id)

    def _load_securities(self):
        logger.debug(f"[TickerSelector] _load_securities: connector_id={self._connector_id}")
        connector = self._get_connector()
        logger.debug(f"[TickerSelector] connector object: {connector}")
        if not connector:
            logger.warning(f"[TickerSelector] connector is None for connector_id={self._connector_id}")
            self._lbl.setText("⚠ не подключён")
            return
        is_connected = connector.is_connected()
        logger.debug(f"[TickerSelector] connector.is_connected()={is_connected}")
        if not is_connected:
            self._lbl.setText("⚠ не подключён")
            return

        self._lbl.setText("⏳")
        self._btn_refresh.setEnabled(False)

        board = self._cmb_board.currentText() if self._cmb_board else "TQBR"
        threading.Thread(
            target=self._fetch,
            args=(board,),
            daemon=True,
        ).start()

    def _fetch(self, board: str):
        """Выполняется в фоновом потоке — результат через сигналы."""
        try:
            connector = self._get_connector()
            if not connector:
                return

            # Для QUIK загружаем классы
            if self._connector_id == "quik" and self._cmb_board is not None:
                classes = connector.get_classes()
                if classes:
                    self._worker.boards_ready.emit(classes)

            securities = connector.get_securities(board)
            self._worker.securities_ready.emit(securities)

        except Exception as e:
            logger.error(f"[TickerSelector] fetch error: {e}")
            self._worker.error.emit(str(e))

    # ─────────────────────────────────────────────
    # Слоты — вызываются в главном потоке через сигналы
    # ─────────────────────────────────────────────

    def _update_boards(self, classes: list):
        if not self._cmb_board:
            return
        current = self._cmb_board.currentText()
        self._cmb_board.blockSignals(True)
        self._cmb_board.clear()
        for cls in classes:
            self._cmb_board.addItem(cls)
        idx = self._cmb_board.findText(current)
        self._cmb_board.setCurrentIndex(idx if idx >= 0 else 0)
        self._cmb_board.blockSignals(False)

    def _update_combo(self, securities: list):
        current = self.ticker()   # запоминаем до очистки

        self._cmb.blockSignals(True)
        self._cmb.clear()
        for sec in securities:
            name    = sec.get("name", "")
            display = f"{sec['ticker']}  —  {name}" if name else sec["ticker"]
            self._cmb.addItem(display, sec["ticker"])
        self._cmb.blockSignals(False)

        # Восстанавливаем тикер
        idx = self._cmb.findData(current)
        if idx >= 0:
            self._cmb.setCurrentIndex(idx)
        elif current:
            self._cmb.setCurrentText(current)

        self._lbl.setText(f"✓ {len(securities)}")
        self._btn_refresh.setEnabled(True)

    def _on_error(self, msg: str):
        self._lbl.setText("⚠ ошибка")
        self._btn_refresh.setEnabled(True)
        logger.warning(f"[TickerSelector] {msg}")

    def _on_board_changed(self, board: str):
        self.board_changed.emit(board)
        self._load_securities()

    # ─────────────────────────────────────────────
    # Публичный API
    # ─────────────────────────────────────────────

    def ticker(self) -> str:
        """Возвращает выбранный тикер."""
        idx  = self._cmb.currentIndex()
        data = self._cmb.itemData(idx)
        if data:
            return str(data)
        # Введён вручную — обрезаем " — Название" если есть
        return self._cmb.currentText().split("  —  ")[0].strip()

    def board(self) -> str:
        """Возвращает выбранный борд."""
        return self._cmb_board.currentText() or "TQBR"

    def set_connector(self, connector_id: str):
        """Переключить коннектор при смене в настройках агента."""
        self._connector_id = connector_id
        # Обновляем список бордов
        self._cmb_board.blockSignals(True)
        self._cmb_board.clear()
        if connector_id == "finam":
            for b in ["TQBR", "FUT", "TQCB", "TQOB", "CETS", "SPBFUT"]:
                self._cmb_board.addItem(b)
        else:
            self._cmb_board.addItem("TQBR")
        self._cmb_board.blockSignals(False)
        self._load_securities()

    def set_ticker_and_board(self, ticker: str, board: str):
        """Программно устанавливает тикер и борд без эмиссии сигналов."""
        self._cmb_board.blockSignals(True)
        idx = self._cmb_board.findText(board)
        if idx >= 0:
            self._cmb_board.setCurrentIndex(idx)
        self._cmb_board.blockSignals(False)

        self._cmb.blockSignals(True)
        self._cmb.setCurrentText(ticker)
        self._cmb.blockSignals(False)
