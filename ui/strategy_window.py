from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget,
    QLabel, QLineEdit, QComboBox, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QSpinBox,
    QDoubleSpinBox, QCheckBox, QTimeEdit, QListWidget,
    QListWidgetItem, QMessageBox, QFormLayout, QGroupBox,
    QSizePolicy, QFrame, QScrollArea,
)
from PyQt6.QtCore import Qt, QTimer, QTime, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from loguru import logger
from core.position_manager import position_manager  # для синхронизации

from core.storage import get_strategy, save_strategy, get_setting, save_setting
from core.strategy_loader import strategy_loader
from core.scheduler import strategy_scheduler, DAYS_RU

STYLE_DIALOG = """
QDialog {
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: 'Segoe UI', Arial;
    font-size: 13px;
}
QTabWidget::pane {
    border: 1px solid #313244;
    border-radius: 6px;
    background-color: #1e1e2e;
}
QTabBar::tab {
    background-color: #181825;
    color: #6c7086;
    padding: 8px 20px;
    border: none;
    border-bottom: 2px solid transparent;
    font-size: 12px;
}
QTabBar::tab:selected {
    color: #89b4fa;
    border-bottom: 2px solid #89b4fa;
    background-color: #1e1e2e;
}
QTabBar::tab:hover { color: #cdd6f4; }
QGroupBox {
    border: 1px solid #313244;
    border-radius: 6px;
    margin-top: 12px;
    padding: 10px;
    color: #89b4fa;
    font-weight: bold;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTimeEdit {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 4px;
    padding: 5px 8px;
    color: #cdd6f4;
    font-size: 13px;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QTimeEdit:focus {
    border: 1px solid #89b4fa;
}
QPushButton {
    background-color: #313244;
    color: #cdd6f4;
    border: none;
    border-radius: 5px;
    padding: 6px 14px;
}
QPushButton:hover  { background-color: #45475a; }
QPushButton:pressed { background-color: #585b70; }
QPushButton#btn_save {
    background-color: #89b4fa;
    color: #1e1e2e;
    font-weight: bold;
}
QPushButton#btn_start {
    background-color: #a6e3a1;
    color: #1e1e2e;
    font-weight: bold;
}
QPushButton#btn_stop {
    background-color: #f38ba8;
    color: #1e1e2e;
    font-weight: bold;
}
QPushButton#btn_buy {
    background-color: #a6e3a1;
    color: #1e1e2e;
    font-weight: bold;
    font-size: 14px;
    padding: 10px;
}
QPushButton#btn_sell {
    background-color: #f38ba8;
    color: #1e1e2e;
    font-weight: bold;
    font-size: 14px;
    padding: 10px;
}
QTableWidget {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 6px;
    gridline-color: #2a2a3e;
}
QHeaderView::section {
    background-color: #11111b;
    color: #89b4fa;
    padding: 5px 8px;
    border: none;
    border-right: 1px solid #313244;
    border-bottom: 1px solid #313244;
    font-weight: bold;
}
QCheckBox { color: #cdd6f4; spacing: 6px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #585b70;
    border-radius: 3px;
    background-color: #181825;
}
QCheckBox::indicator:checked {
    background-color: #89b4fa;
    border-color: #89b4fa;
}
QLabel#lbl_title {
    font-size: 15px;
    font-weight: bold;
    color: #89b4fa;
}
QLabel#lbl_status_active { color: #a6e3a1; font-weight: bold; }
QLabel#lbl_status_stopped { color: #f38ba8; font-weight: bold; }
"""


class _InstrumentsWidget(QWidget):
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

        btn = QPushButton("✏ Редактировать")
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


class StrategyWindow(QDialog):
    """
    Окно настройки агента. Открывается по двойному клику в таблице.
    Вкладки: Обзор | Параметры | Позиции | Ручной ордер | Расписание
    """

    strategy_updated = pyqtSignal(str)  # strategy_id — для обновления таблицы

    def __init__(self, strategy_id: str, parent=None):
        super().__init__(parent)
        self.sid = strategy_id
        self.data = get_strategy(strategy_id) or {}
        self.loaded = strategy_loader.get(strategy_id)
        
        # Если стратегия не загружена, загружаем её
        if not self.loaded:
            file_path = self.data.get("file_path", "")
            if file_path:
                try:
                    self.loaded = strategy_loader.load(strategy_id, file_path)
                    logger.info(f"[StrategyWindow] Стратегия [{strategy_id}] загружена из {file_path}")
                except Exception as e:
                    logger.error(f"[StrategyWindow] Не удалось загрузить стратегию [{strategy_id}]: {e}")
                    self.loaded = None

        # True если стратегия использует корзину инструментов (instruments)
        # В этом случае тикер не нужен на Обзоре, комиссия — в % от суммы
        # Для обычных стратегий (Валера, Трекер) — тикер на Обзоре, комиссия в рублях
        schema = self.loaded.params_schema if self.loaded else {}
        self._has_instruments = any(
            v.get("type") == "instruments" for v in schema.values()
        )

        name = self.data.get("name", strategy_id)
        self.setWindowTitle(f"Агент: {name}")
        self.setMinimumSize(680, 540)
        self.resize(780, 600)
        self.setStyleSheet(STYLE_DIALOG)

        self._build_ui()

    # ─────────────────────────────────────────────
    # Построение UI
    # ─────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_header())

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self.tab_overview(), "📋  Обзор")
        self.tabs.addTab(self.tab_params(), "⚙  Параметры")
        self.tabs.addTab(self._tab_lot_sizing(), "📐  Лотность")
        self.tabs.addTab(self._tab_positions(), "📊  Позиции")
        self.tabs.addTab(self._tab_order(), "💹  Ручной ордер")
        layout.addWidget(self.tabs, stretch=1)

        self._sync_tickers()

        # Блокируем редактирование если агент запущен
        if self.data.get("status") == "active":
            self._lock_editing()

    def _lock_editing(self):
        """Блокирует редактирование вкладок Обзор и Параметры если агент запущен."""
        for tab_idx in (0, 1):
            widget = self.tabs.widget(tab_idx)
            if not widget:
                continue
            for child in widget.findChildren(
                (QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QCheckBox, QTimeEdit)
            ):
                child.setEnabled(False)
            for btn in widget.findChildren(QPushButton):
                if "Сохранить" in btn.text():
                    btn.setEnabled(False)

    def _sync_tickers(self):
        from ui.ticker_selector import TickerSelector
        from ui.param_widgets import TickerParamWidget

        selectors: list[TickerSelector] = []
        # Тикер на вкладке Обзор убран — синхронизируем только Параметры и Ручной ордер
        params_sel = self._param_widgets.get("ticker") if hasattr(self, "_param_widgets") else None
        
        # Поддержка как TickerParamWidget, так и старого TickerSelector
        if isinstance(params_sel, TickerParamWidget):
            # Извлекаем внутренний TickerSelector из TickerParamWidget
            selectors.append(params_sel.ticker_selector)
        elif isinstance(params_sel, TickerSelector):
            selectors.append(params_sel)
            
        order_sel = getattr(self, "_order_ticker_sel", None)
        if order_sel:
            selectors.append(order_sel)

        if len(selectors) < 2:
            return

        self._syncing = False

        def _broadcast_ticker(source: TickerSelector, ticker: str):
            if self._syncing:
                return
            self._syncing = True
            board = source.board()
            for sel in selectors:
                if sel is not source:
                    sel.set_ticker_and_board(ticker, board)
            self._syncing = False

        def _broadcast_board(source: TickerSelector, board: str):
            if self._syncing:
                return
            self._syncing = True
            ticker = source.ticker()
            for sel in selectors:
                if sel is not source:
                    sel.set_ticker_and_board(ticker, board)
            self._syncing = False

        for sel in selectors:
            s = sel  # capture
            sel.ticker_changed.connect(lambda t, src=s: _broadcast_ticker(src, t))
            sel.board_changed.connect(lambda b, src=s: _broadcast_board(src, b))

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setFixedHeight(52)
        header.setStyleSheet(
            "background-color: #181825; border-bottom: 1px solid #313244;"
        )
        layout = QHBoxLayout(header)
        layout.setContentsMargins(16, 6, 16, 6)
        layout.setSpacing(10)

        name = self.data.get("name", self.sid)
        lbl_name = QLabel(name)
        lbl_name.setObjectName("lbl_title")
        lbl_name.setMaximumWidth(300)  # ← ограничиваем ширину
        layout.addWidget(lbl_name)

        status = self.data.get("status", "stopped")
        self.lbl_status = QLabel(
            "🟢 Активен" if status == "active" else "🔴 Остановлен"
        )
        self.lbl_status.setStyleSheet(
            "color: #a6e3a1; font-weight: bold;"
            if status == "active"
            else "color: #f38ba8; font-weight: bold;"
        )
        layout.addWidget(self.lbl_status)

        # Кумулятивный P&L
        from core.order_history import get_total_pnl
        pnl = get_total_pnl(self.sid)
        if pnl is not None:
            pnl_text = f"P&L: {pnl:+.2f} ₽"
            pnl_color = "#a6e3a1" if pnl >= 0 else "#f38ba8"
        else:
            pnl_text = "P&L: —"
            pnl_color = "#6c7086"
        self.lbl_pnl = QLabel(pnl_text)
        self.lbl_pnl.setStyleSheet(f"color: {pnl_color}; font-weight: bold;")
        layout.addWidget(self.lbl_pnl)

        layout.addStretch()

        self.btn_start = QPushButton("▶ Запустить")
        self.btn_start.setObjectName("btn_start")
        self.btn_start.setFixedSize(110, 34)
        self.btn_start.clicked.connect(self._start_strategy)
        layout.addWidget(self.btn_start)

        self.btn_stop = QPushButton("■ Остановить")
        self.btn_stop.setObjectName("btn_stop")
        self.btn_stop.setFixedSize(110, 34)
        self.btn_stop.clicked.connect(self._stop_strategy)
        layout.addWidget(self.btn_stop)

        return header

    # ─────────────────────────────────────────────
    # Вкладка: Обзор
    # ─────────────────────────────────────────────

    # Найди метод tab_overview и замени его целиком на этот:

    def tab_overview(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        # ── Информация о стратегии ──────────────────
        info_group = QGroupBox("Информация")
        form = QFormLayout(info_group)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        desc = self.data.get("description") or (self.loaded.info.get("description", "") if self.loaded else "")
        version = self.loaded.info.get("version", "") if self.loaded else ""
        filepath = self.data.get("file_path", "")

        # Название агента (редактируемое)
        self.edit_agent_name = QLineEdit()
        self.edit_agent_name.setText(self.data.get("name", self.sid))
        self.edit_agent_name.setPlaceholderText("Название агента")
        form.addRow("Название:", self.edit_agent_name)

        form.addRow("Версия:", QLabel(str(version)))
        form.addRow("Описание:", QLabel(desc))
        form.addRow("Файл:", QLabel(filepath))
        layout.addWidget(info_group)

        # ── Торговые параметры ──────────────────────
        trade_group = QGroupBox("Торговые параметры")
        trade_form = QFormLayout(trade_group)
        trade_form.setSpacing(10)
        trade_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # Коннектор
        self.cmb_connector = QComboBox()
        self.cmb_connector.addItem("🏦  Финам (TransAQ)", "finam")
        self.cmb_connector.addItem("🖥  QUIK", "quik")
        current_connector = self.data.get("connector", "finam")
        idx = self.cmb_connector.findData(current_connector)
        if idx >= 0:
            self.cmb_connector.setCurrentIndex(idx)
        self.cmb_connector.currentIndexChanged.connect(self._on_connector_changed)
        trade_form.addRow("Коннектор:", self.cmb_connector)

        # Счёт
        self.cmb_account = QComboBox()
        self._refresh_accounts()
        trade_form.addRow("Счёт:", self.cmb_account)

        layout.addWidget(trade_group)

        # ── Инструменты (если стратегия использует корзину) ──────────────────────
        if self._has_instruments:
            instruments_group = QGroupBox("Инструменты")
            instruments_layout = QHBoxLayout(instruments_group)
            # Get the effective instruments
            default_instruments = []
            if self.loaded:
                schema = self.loaded.params_schema
                if "instruments" in schema:
                    default_instruments = schema["instruments"].get("default", [])
            instruments = self.data.get("params", {}).get("instruments", default_instruments)
            # Use the _InstrumentsWidget to display
            connector_id = self.data.get("connector_id", self.data.get("connector", "finam"))
            instruments_widget = _InstrumentsWidget(connector_id, instruments, self)
            instruments_layout.addWidget(instruments_widget)
            layout.addWidget(instruments_group)

        layout.addStretch()

        btn_save = QPushButton("💾  Сохранить")
        btn_save.setObjectName("btn_save")
        btn_save.setFixedWidth(140)
        btn_save.clicked.connect(self.save_overview)
        layout.addWidget(btn_save, alignment=Qt.AlignmentFlag.AlignRight)

        return tab

    def _on_connector_changed(self):
        """При смене коннектора обновляем счета и тикер-селектор."""
        cid = self.cmb_connector.currentData()
        self._refresh_accounts()
        if hasattr(self, "_ticker_sel"):
            self._ticker_sel.set_connector(cid)

    def _refresh_accounts(self):
        """Обновляет список счетов при смене коннектора."""
        from core.connector_manager import connector_manager
        from core.storage import get_setting
        cid = self.cmb_connector.currentData()
        connector = connector_manager.get(cid)
        
        # Проверяем подключение коннектора
        connected = connector and connector.is_connected()
        accounts = connector.get_accounts() if connected else []
        
        # Логирование для диагностики
        logger.debug(f"[StrategyWindow] _refresh_accounts: connector_id={cid}, connected={connected}, accounts={len(accounts)}")
        
        # Если подключены — обновляем кэш known_accounts
        known_accounts = get_setting("known_accounts") or {}
        if connected and accounts:
            known_accounts[cid] = [a.get("id", "") for a in accounts if a.get("id")]
            save_setting("known_accounts", known_accounts)
            logger.debug(f"[StrategyWindow] Обновлен кэш known_accounts для {cid}: {known_accounts[cid]}")
        
        # Fallback: если не подключён — берём из кэша known_accounts
        if not accounts:
            cached_ids = known_accounts.get(cid, [])
            accounts = [{"id": acc_id} for acc_id in cached_ids if acc_id]
            if accounts:
                logger.debug(f"[StrategyWindow] Используем кэшированные счета для {cid}: {cached_ids}")
        
        aliases = get_setting("account_aliases") or {}

        self.cmb_account.clear()
        self.cmb_account.addItem("— не выбран —", "")
        for acc in accounts:
            acc_id = acc.get("id", "")
            if not acc_id:
                continue
            alias = aliases.get(acc_id, "")
            label = alias if alias else acc_id
            self.cmb_account.addItem(label, acc_id)

        # Восстанавливаем сохранённый счёт
        current_acc = self.data.get("finam_account", "")
        idx = self.cmb_account.findData(current_acc)
        if idx >= 0:
            self.cmb_account.setCurrentIndex(idx)

    # ─────────────────────────────────────────────
    # Вкладка: Параметры
    # ─────────────────────────────────────────────

    def tab_params(self) -> QWidget:
        tab = QWidget()
        main_layout = QVBoxLayout(tab)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        if not self.loaded:
            main_layout.addWidget(QLabel("Стратегия не загружена"))
            return tab

        # Создаём QScrollArea
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        
        # Создаём контейнерный виджет для содержимого
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        schema = self.loaded.params_schema
        params = self.data.get("params", {})
        connector_id = self.data.get("connector", "finam")
        
        logger.debug(f"[StrategyWindow] tab_params: connector_id={connector_id}, "
                     f"strategy_name={self.data.get('name', 'unknown')}")

        self._param_widgets: dict = {}
        self._commission_widget = None

        group = QGroupBox("Параметры стратегии")
        form = QFormLayout(group)
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        from ui.param_widgets import ParamWidgetFactory, CommissionParamWidget

        for key, meta in schema.items():
            # Пропускаем параметр board - он уже включен в TickerParamWidget
            if key == "board":
                continue
                
            label = meta.get("label", key)
            default = meta.get("default", "")
            current = params.get(key, default)

            # Специальная обработка для ticker с board
            if key == "ticker":
                # Для ticker передаём board в метаданные для TickerParamWidget
                meta_with_board = meta.copy()
                meta_with_board["board"] = params.get("board", "TQBR")
                widget = ParamWidgetFactory.create(key, meta_with_board, current, connector_id)
                
                self._param_widgets[key] = widget
                # НЕ добавляем в _param_widgets["board"] — это семантически неверно
                form.addRow(f"{label}:", widget)
                
                # Подключаем сигнал смены борды к CommissionParamWidget.
                # _make_board_slot явно захватывает win, чтобы _commission_widget
                # читался динамически — он создаётся позже в том же цикле.
                if hasattr(widget, "ticker_selector"):
                    def _make_board_slot(win):
                        def _slot(board):
                            cw = getattr(win, "_commission_widget", None)
                            if cw is not None:
                                cw.set_board_type("FUT" in board.upper())
                        return _slot
                    widget.ticker_selector.board_changed.connect(_make_board_slot(self))
                
                continue

            # Создаём виджет через фабрику
            widget = ParamWidgetFactory.create(key, meta, current, connector_id)
            
            self._param_widgets[key] = widget
            form.addRow(f"{label}:", widget)
            
            # Если это виджет комиссии, сохраняем ссылку
            if isinstance(widget, CommissionParamWidget):
                self._commission_widget = widget

        # Устанавливаем начальное состояние виджета комиссии
        if self._commission_widget is not None:
            board = params.get("board", "TQBR")
            self._commission_widget.set_board_type("FUT" in board.upper())

        layout.addWidget(group)
        layout.addStretch()

        btn = QPushButton("💾  Сохранить параметры")
        btn.setObjectName("btn_save")
        btn.setFixedWidth(180)
        btn.clicked.connect(self.save_params)
        layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignRight)

        # Устанавливаем контейнер в scroll area
        scroll.setWidget(container)
        main_layout.addWidget(scroll)

        return tab

    # ─────────────────────────────────────────────
    # Вкладка: Лотность
    # ─────────────────────────────────────────────

    def _tab_lot_sizing(self) -> QWidget:
        tab = QWidget()
        main_layout = QVBoxLayout(tab)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Создаём QScrollArea
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        
        # Создаём контейнерный виджет для содержимого
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        lot_data = self.data.get("lot_sizing", {})

        # ── Динамический лот ──────────────────────
        group = QGroupBox("Управление лотностью")
        form = QFormLayout(group)
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.chk_dynamic_lot = QCheckBox("Динамический лот?")
        self.chk_dynamic_lot.setChecked(lot_data.get("dynamic", False))
        self.chk_dynamic_lot.toggled.connect(self._on_dynamic_lot_toggled)
        form.addRow("", self.chk_dynamic_lot)

        # Экземпляры
        self.spin_instances = QSpinBox()
        self.spin_instances.setRange(1, 100)
        self.spin_instances.setValue(int(lot_data.get("instances", 1)))
        self.spin_instances.setFixedWidth(120)
        self.spin_instances.setToolTip("Сколько экземпляров стратегии запущено на этом счёте")
        form.addRow("Экземпляры:", self.spin_instances)

        # Просадка (ручная)
        self.spin_drawdown = QDoubleSpinBox()
        self.spin_drawdown.setRange(0, 10_000_000)
        self.spin_drawdown.setDecimals(2)
        self.spin_drawdown.setValue(float(lot_data.get("drawdown", 0)))
        self.spin_drawdown.setFixedWidth(160)
        self.spin_drawdown.setToolTip("Ручная просадка в валюте на 1 лот. "
                                       "Если больше просадки по стратегии — используется эта.")
        form.addRow("Просадка:", self.spin_drawdown)

        # Просадка по стратегии (нередактируемое)
        from core.equity_tracker import get_max_drawdown
        strat_dd = get_max_drawdown(self.sid)
        dd_text = f"{strat_dd:.2f}" if strat_dd else "—"
        self.lbl_strat_drawdown = QLabel(dd_text)
        self.lbl_strat_drawdown.setStyleSheet(
            "background-color: #181825; border: 1px solid #313244; "
            "border-radius: 4px; padding: 5px 8px; color: #f38ba8; font-weight: bold;"
        )
        self.lbl_strat_drawdown.setFixedWidth(160)
        self.lbl_strat_drawdown.setToolTip("Макс. просадка в валюте на 1 лот за всё время работы агента")
        form.addRow("Просадка по стратегии:", self.lbl_strat_drawdown)

        layout.addWidget(group)

        # ── Предпросмотр расчёта ──────────────────
        self._lot_preview_group = QGroupBox("Расчёт динамического лота")
        preview_form = QFormLayout(self._lot_preview_group)
        preview_form.setSpacing(8)

        self.lbl_free_money = QLabel("—")
        self.lbl_free_money.setStyleSheet("color: #a6e3a1; font-weight: bold;")
        preview_form.addRow("Свободные средства:", self.lbl_free_money)

        self.lbl_go_buy = QLabel("—")
        preview_form.addRow("ГО покупателя:", self.lbl_go_buy)

        self.lbl_go_sell = QLabel("—")
        preview_form.addRow("ГО продавца:", self.lbl_go_sell)

        self.lbl_calc_lot_buy = QLabel("—")
        self.lbl_calc_lot_buy.setStyleSheet("color: #a6e3a1; font-size: 14px; font-weight: bold;")
        preview_form.addRow("Лот (покупка):", self.lbl_calc_lot_buy)

        self.lbl_calc_lot_sell = QLabel("—")
        self.lbl_calc_lot_sell.setStyleSheet("color: #f38ba8; font-size: 14px; font-weight: bold;")
        preview_form.addRow("Лот (продажа):", self.lbl_calc_lot_sell)

        layout.addWidget(self._lot_preview_group)

        # Обновить состояние виджетов
        self._on_dynamic_lot_toggled(self.chk_dynamic_lot.isChecked())

        # Кнопки
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        btn_refresh = QPushButton("🔄  Обновить расчёт")
        btn_refresh.setFixedWidth(160)
        btn_refresh.clicked.connect(self._refresh_lot_preview)
        btn_row.addWidget(btn_refresh)

        btn_save = QPushButton("💾  Сохранить")
        btn_save.setObjectName("btn_save")
        btn_save.setFixedWidth(140)
        btn_save.clicked.connect(self._save_lot_sizing)
        btn_row.addWidget(btn_save)

        layout.addLayout(btn_row)
        layout.addStretch()

        # Устанавливаем контейнер в scroll area
        scroll.setWidget(container)
        main_layout.addWidget(scroll)

        # Автообновление при открытии
        QTimer.singleShot(500, self._refresh_lot_preview)

        # Таймер автообновления каждые 60 секунд
        self._lot_timer = QTimer(self)
        self._lot_timer.setInterval(60_000)
        self._lot_timer.timeout.connect(self._refresh_lot_preview)
        self._lot_timer.start()

        return tab

    def _on_dynamic_lot_toggled(self, checked: bool):
        """Переключение динамического лота."""
        self._lot_preview_group.setVisible(checked)

    def _refresh_lot_preview(self):
        """Обновляет предпросмотр расчёта динамического лота."""
        from core.connector_manager import connector_manager
        from core.equity_tracker import get_max_drawdown
        import math

        connector_id = self.data.get("connector", "finam")
        connector = connector_manager.get(connector_id)
        account = self.data.get("finam_account", "")
        ticker = self.data.get("ticker") or self.data.get("params", {}).get("ticker", "")
        board = self.data.get("board", "FUT")

        # Свободные средства
        free_money = None
        if connector and account:
            free_money = connector.get_free_money(account)
        self.lbl_free_money.setText(f"{free_money:,.2f}" if free_money is not None else "—")

        # ГО
        go_buy = None
        go_sell = None
        if connector and connector.is_connected() and ticker:
            sec_info = connector.get_sec_info(ticker, board)
            if sec_info:
                go_buy = sec_info.get("buy_deposit")
                go_sell = sec_info.get("sell_deposit")
        self.lbl_go_buy.setText(f"{go_buy:,.2f}" if go_buy else "—")
        self.lbl_go_sell.setText(f"{go_sell:,.2f}" if go_sell else "—")

        # Просадка по стратегии
        strat_dd = get_max_drawdown(self.sid)
        self.lbl_strat_drawdown.setText(f"{strat_dd:.2f}" if strat_dd else "—")

        # Просадка: max(ручная, по стратегии)
        manual_dd = self.spin_drawdown.value()
        effective_dd = max(manual_dd, strat_dd or 0)

        instances = self.spin_instances.value()

        # Расчёт лота
        if free_money is not None and go_buy and effective_dd >= 0 and instances > 0:
            denom_buy = effective_dd + go_buy
            lot_buy = math.floor((free_money / denom_buy) / instances) if denom_buy > 0 else 0
            self.lbl_calc_lot_buy.setText(str(max(lot_buy, 0)))
        else:
            self.lbl_calc_lot_buy.setText("—")

        if free_money is not None and go_sell and effective_dd >= 0 and instances > 0:
            denom_sell = effective_dd + go_sell
            lot_sell = math.floor((free_money / denom_sell) / instances) if denom_sell > 0 else 0
            self.lbl_calc_lot_sell.setText(str(max(lot_sell, 0)))
        else:
            self.lbl_calc_lot_sell.setText("—")

    def _is_futures_board(self) -> bool:
        """Определяет, является ли текущий борд фьючерсным."""
        if self._has_instruments:
            # Для стратегий с корзиной инструментов используем процентную комиссию
            return False
        
        # Получаем текущий борд из ticker_selector на вкладке Обзор
        if hasattr(self, "_ticker_sel"):
            board = self._ticker_sel.board()
            return "FUT" in board.upper()
        
        # Если ticker_selector недоступен, проверяем сохранённый борд
        board = self.data.get("board", "TQBR")
        return "FUT" in board.upper()

    def _on_board_changed_commission(self, board: str):
        """Обработчик изменения борды для виджета комиссии на вкладке Параметры."""
        if hasattr(self, "_commission_widget"):
            is_futures = "FUT" in board.upper()
            self._commission_widget.set_board_type(is_futures)

    def _save_lot_sizing(self):
        lot_data = {
            "dynamic": self.chk_dynamic_lot.isChecked(),
            "instances": self.spin_instances.value(),
            "drawdown": self.spin_drawdown.value(),
        }
        
        self.data["lot_sizing"] = lot_data
        save_strategy(self.sid, self.data)
        logger.info(f"{self.sid}: лотность сохранена")
        self.strategy_updated.emit(self.sid)

    # ─────────────────────────────────────────────
    # Вкладка: Позиции
    # ─────────────────────────────────────────────

    def _tab_positions(self) -> QWidget:
        from ui.positions_panel import PositionsPanel
        from core.autostart import get_live_engines

        account = self.data.get("finam_account") or None
        ticker = self.data.get("ticker") or self.data.get("params", {}).get("ticker") or None
        engine = get_live_engines().get(self.sid)
        self._positions_panel = PositionsPanel(
            account_id=account, ticker=ticker, live_engine=engine, parent=self
        )
        return self._positions_panel

    # ─────────────────────────────────────────────
    # Вкладка: Ручной ордер
    # ─────────────────────────────────────────────

    def _tab_order(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(30, 20, 30, 20)
        layout.setSpacing(16)

        order_group = QGroupBox("Параметры ордера")
        form = QFormLayout(order_group)
        form.setSpacing(12)

        params = self.data.get("params", {})
        connector_id = self.data.get("connector", "finam")

        # Тикер (TickerSelector — с выбором борда и автозагрузкой тикеров)
        from ui.ticker_selector import TickerSelector
        self._order_ticker_sel = TickerSelector(
            connector_id=connector_id,
            current_ticker=self.data.get("ticker", "") or params.get("ticker", ""),
            current_board=self.data.get("board", "TQBR"),
        )
        form.addRow("Тикер:", self._order_ticker_sel)

        # Тип ордера
        self.order_type = QComboBox()
        self.order_type.addItem("Рыночный", "market")
        self.order_type.addItem("Лимитный", "limit")
        self.order_type.currentIndexChanged.connect(self._on_order_type_changed)
        form.addRow("Тип:", self.order_type)

        # Цена (только для лимитных)
        self.order_price = QDoubleSpinBox()
        self.order_price.setRange(0, 999999)
        self.order_price.setDecimals(4)
        self.order_price.setEnabled(False)
        form.addRow("Цена:", self.order_price)

        # Количество
        self.order_qty = QSpinBox()
        self.order_qty.setRange(1, 100000)
        self.order_qty.setValue(1)
        form.addRow("Лотов:", self.order_qty)

        layout.addWidget(order_group)

        # Кнопки BUY / SELL
        btn_row = QHBoxLayout()
        btn_row.setSpacing(16)

        btn_buy = QPushButton("▲  КУПИТЬ")
        btn_buy.setObjectName("btn_buy")
        btn_buy.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        btn_buy.setFixedHeight(48)
        btn_buy.clicked.connect(lambda: self._place_order("buy"))
        btn_row.addWidget(btn_buy)

        btn_sell = QPushButton("▼  ПРОДАТЬ")
        btn_sell.setObjectName("btn_sell")
        btn_sell.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        btn_sell.setFixedHeight(48)
        btn_sell.clicked.connect(lambda: self._place_order("sell"))
        btn_row.addWidget(btn_sell)

        layout.addLayout(btn_row)
        layout.addStretch()

        # Последний ордер
        self.lbl_last_order = QLabel("Последний ордер: —")
        self.lbl_last_order.setStyleSheet("color: #6c7086; font-size: 12px;")
        layout.addWidget(self.lbl_last_order)

        return tab

    def _on_order_type_changed(self, idx: int):
        is_limit = self.order_type.currentData() == "limit"
        self.order_price.setEnabled(is_limit)

    def _place_order(self, side: str):
        account = self.data.get("finam_account", "")
        if not account:
            QMessageBox.warning(self, "Нет счёта",
                                "Привяжи счёт на вкладке «Обзор»")
            return

        from core.connector_manager import connector_manager
        connector_id = self.data.get("connector", "finam")
        connector = connector_manager.get(connector_id)
        if not connector or not connector.is_connected():
            QMessageBox.warning(self, "Нет подключения",
                                f"Коннектор [{connector_id}] не подключён")
            return

        ticker    = self._order_ticker_sel.ticker().strip().upper()
        board     = self._order_ticker_sel.board()
        order_type = self.order_type.currentData()
        price     = self.order_price.value() if order_type == "limit" else 0.0
        qty       = self.order_qty.value()

        if not ticker:
            QMessageBox.warning(self, "Тикер", "Введи тикер инструмента")
            return

        confirm = QMessageBox.question(
            self,
            "Подтвердить ордер",
            f"{side.upper()} {ticker}  x{qty}  "
            f"@ {'рыночная' if order_type == 'market' else price}\nСчёт: {account}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        tid = connector.place_order(
            account_id=account,
            ticker=ticker,
            side=side,
            quantity=qty,
            order_type=order_type,
            price=price,
            board=board,
            agent_name=self.sid,
        )
        if tid:
            self.lbl_last_order.setText(
                f"Ордер принят: {side.upper()} {ticker} x{qty} → transactionid={tid}"
            )
            self.lbl_last_order.setStyleSheet("color: #a6e3a1; font-size: 12px;")
        else:
            self.lbl_last_order.setText(f"Ордер отклонён: {side.upper()} {ticker}")
            self.lbl_last_order.setStyleSheet("color: #f38ba8; font-size: 12px;")

    # ─────────────────────────────────────────────
    # Сохранение
    # ─────────────────────────────────────────────

    def save_overview(self):
        self.data["name"] = self.edit_agent_name.text().strip() or self.sid
        self.data["connector"] = self.cmb_connector.currentData()
        self.data["finam_account"] = self.cmb_account.currentData() or ""
        # Тикер/борд больше не сохраняются из вкладки Обзор
        # Они должны сохраняться из вкладки Параметры
        save_strategy(self.sid, self.data)

        # Синхронизируем панель позиций
        if hasattr(self, "_positions_panel"):
            self._positions_panel.set_account(self.data["finam_account"])

        logger.info(f"{self.sid}: обзор сохранён [{self.data['connector']}]")
        self.strategy_updated.emit(self.sid)

    def save_params(self):
        if not self.loaded:
            return
        
        from ui.param_widgets import BaseParamWidget, TickerParamWidget
        
        schema = self.loaded.params_schema
        params = {}
        
        for key, meta in schema.items():
            widget = self._param_widgets.get(key)
            if widget is None:
                continue
            
            # Используем BaseParamWidget.get_value() для всех виджетов
            if isinstance(widget, BaseParamWidget):
                params[key] = widget.get_value()
                
                # Специальная обработка для ticker - сохраняем board
                if isinstance(widget, TickerParamWidget):
                    params["board"] = widget.get_board()
            else:
                # Fallback для старых виджетов (обратная совместимость)
                ptype = meta.get("type", "str")
                
                if key == "ticker":
                    from ui.ticker_selector import TickerSelector
                    if isinstance(widget, TickerSelector):
                        params[key] = widget.ticker()
                        params["board"] = widget.board()
                    else:
                        params[key] = widget.text()
                elif ptype == "bool":
                    params[key] = widget.isChecked()
                elif ptype == "time":
                    t = widget.time()
                    params[key] = t.hour() * 60 + t.minute()
                elif ptype == "int":
                    params[key] = widget.value()
                elif ptype == "float":
                    params[key] = widget.value()
                elif ptype == "instruments":
                    params[key] = widget.get_value()
                elif ptype in ("choice", "select"):
                    params[key] = widget.currentData()
                else:
                    params[key] = widget.text()

        self.data["params"] = params

        # Синхронизируем верхнеуровневые ticker/board с params,
        # чтобы главная таблица, LiveEngine и графики видели актуальные значения
        if "ticker" in params:
            self.data["ticker"] = params["ticker"]
        if "board" in params:
            self.data["board"] = params["board"]

        save_strategy(self.sid, self.data)
        logger.info(f"{self.sid}: параметры сохранены")
        self.strategy_updated.emit(self.sid)

    # ─────────────────────────────────────────────
    # Управление стратегией
    # ─────────────────────────────────────────────

    def _start_strategy(self):
        self.data["status"] = "active"
        save_strategy(self.sid, self.data)
        self.lbl_status.setText("🟢 Активен")
        self.lbl_status.setObjectName("lbl_status_active")
        self.lbl_status.setStyleSheet("color: #a6e3a1; font-weight: bold;")
        logger.info(f"[{self.sid}] Запущен из окна агента")
        self.strategy_updated.emit(self.sid)

    def _stop_strategy(self):
        self.data["status"] = "stopped"
        save_strategy(self.sid, self.data)
        self.lbl_status.setText("🔴 Остановлен")
        self.lbl_status.setObjectName("lbl_status_stopped")
        self.lbl_status.setStyleSheet("color: #f38ba8; font-weight: bold;")
        logger.info(f"[{self.sid}] Остановлен из окна агента")
        self.strategy_updated.emit(self.sid)

    # ─────────────────────────────────────────────
    # Таймер обновления позиций
    # ─────────────────────────────────────────────

    def _start_refresh_timer(self):
        self._timer = QTimer()
        self._timer.timeout.connect(self._refresh_positions)
        self._timer.start(3000)  # Каждые 3 сек

    def closeEvent(self, event):
        event.accept()
