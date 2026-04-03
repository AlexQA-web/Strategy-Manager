import sys
import threading
from datetime import datetime
from html import escape
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTextEdit, QFrame, QFileDialog,
    QMessageBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QSizePolicy, QStatusBar, QTabWidget, QTabBar,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QSettings
from PyQt6.QtGui import QColor, QKeySequence, QShortcut, QIcon
from ui.icons import apply_icon
from loguru import logger

from config.settings import APP_NAME, APP_VERSION
from core.storage import (
    get_all_strategies, save_strategy, save_setting,
    delete_strategy, get_strategy, get_setting, get_bool_setting,
)
from core.strategy_loader import strategy_loader, StrategyLoadError
from core.telegram_bot import notifier
from core.scheduler import strategy_scheduler
from core.finam_connector import finam_connector
from core.quik_connector  import quik_connector
from core.order_history import (
    get_total_pnl, get_pnl_by_ticker, get_open_commission,
    get_total_commission, clear_orders,
)

# ─────────────────────────────────────────────
# Тёмная тема
# ─────────────────────────────────────────────
STYLE = """
QMainWindow, QWidget {
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: 'Segoe UI', Arial;
    font-size: 13px;
}
QTableWidget {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 6px;
    gridline-color: #2a2a3e;
    selection-background-color: #313244;
    selection-color: #cdd6f4;
}
QTableWidget::item {
    padding: 4px 8px;
    border: none;
}
QTableWidget::item:selected { background-color: #313244; }
QHeaderView::section {
    background-color: #11111b;
    color: #89b4fa;
    padding: 6px 8px;
    border: none;
    border-right: 1px solid #313244;
    border-bottom: 1px solid #313244;
    font-weight: bold;
    font-size: 12px;
}
QHeaderView::section:hover { background-color: #1e1e2e; }
QPushButton {
    background-color: #313244;
    color: #cdd6f4;
    border: none;
    border-radius: 5px;
    padding: 6px 14px;
    font-size: 13px;
}
QPushButton:hover   { background-color: #45475a; }
QPushButton:pressed { background-color: #585b70; }
QPushButton#btn_connect {
    background-color: #a6e3a1;
    color: #1e1e2e;
    font-weight: bold;
}
QPushButton#btn_connect:hover   { background-color: #8ed490; }
QPushButton#btn_connect:pressed { background-color: #72c07a; }
QPushButton#btn_disconnect {
    background-color: #f38ba8;
    color: #1e1e2e;
    font-weight: bold;
}
QPushButton#btn_disconnect:hover   { background-color: #e07090; }
QPushButton#btn_disconnect:pressed { background-color: #c05070; }
QPushButton#btn_add {
    background-color: #1e90ff;
    color: #ffffff;
    font-weight: bold;
}
QPushButton#btn_add:hover   { background-color: #1a7de0; }
QPushButton#btn_add:pressed { background-color: #1570c8; }
QPushButton#btn_start {
    background-color: #a6e3a1;
    color: #1e1e2e;
    font-weight: bold;
}
QPushButton#btn_start:hover   { background-color: #8ed490; }
QPushButton#btn_start:pressed { background-color: #72c07a; }
QPushButton#btn_stop {
    background-color: #f38ba8;
    color: #1e1e2e;
    font-weight: bold;
}
QPushButton#btn_stop:hover   { background-color: #e07090; }
QPushButton#btn_stop:pressed { background-color: #c05070; }
QPushButton#btn_log_toggle {
    background-color: transparent;
    color: #89b4fa;
    border: 1px solid #313244;
    border-radius: 4px;
    padding: 2px 10px;
    font-size: 11px;
}
QTextEdit {
    background-color: #11111b;
    border: none;
    padding: 4px 8px;
    font-family: 'Consolas', monospace;
    font-size: 11px;
    color: #cdd6f4;
}
QFrame#topbar  { background-color: #181825; border-bottom: 1px solid #313244; }
QFrame#logbar  { background-color: #11111b; border-top:    1px solid #313244; }
QStatusBar {
    background-color: #11111b;
    color: #6c7086;
    border-top: 1px solid #313244;
    font-size: 11px;
}
QScrollBar:vertical {
    background: #181825; width: 8px; border-radius: 4px;
}
QScrollBar::handle:vertical {
    background: #45475a; border-radius: 4px; min-height: 20px;
}
QScrollBar:horizontal {
    background: #181825; height: 8px; border-radius: 4px;
}
QScrollBar::handle:horizontal {
    background: #45475a; border-radius: 4px; min-width: 20px;
}
QTabWidget::pane {
    border: none;
    background-color: #252535;
}
QTabWidget::tab-bar {
    alignment: left;
}
QTabBar {
    background-color: #11111b;
    border-bottom: 1px solid #313244;
}
QTabBar::tab {
    background-color: #181825;
    color: #6c7086;
    padding: 6px 16px;
    min-width: 100px;
    border: none;
    border-right: 1px solid #313244;
    border-bottom: 2px solid transparent;
    font-size: 13px;
}
QTabBar::tab:selected {
    background-color: #252535;
    color: #cdd6f4;
    border-bottom: 2px solid #89b4fa;
}
QTabBar::tab:hover:!selected {
    background-color: #1e1e2e;
    color: #cdd6f4;
}
QTabBar::tab:first {
    border-left: none;
}
QTabBar::close-button {
    subcontrol-position: right;
    background: transparent;
    border-radius: 3px;
    width: 16px;
    height: 16px;
}
QTabBar::close-button:hover {
    background-color: #f38ba8;
}
"""

# ─────────────────────────────────────────────
# Колонки
# ─────────────────────────────────────────────
COLUMNS = [
    ("Агент",             240),   # шире — под кнопки
    ("Тикер",              80),
    ("Счёт",              120),
    ("Состояние",         120),
    ("Позиция",            70),
    ("П.Лот",              60),
    ("Уч. цена",           90),
    ("Текущая",            90),
    ("П/У",                85),
    ("Комиссия",          100),
    ("Итог П/У",           95),   # нарастающий итог
    ("Итого комиссия",    120),
    ("История ордеров",   110),
    ("📈",                  44),
]

COL = {name: i for i, (name, _) in enumerate(COLUMNS)}


# ─────────────────────────────────────────────
# Сигналы
# ─────────────────────────────────────────────
class UISignals(QObject):
    log_message           = pyqtSignal(str, str)
    connector_changed     = pyqtSignal(str, bool)  # (connector_id, connected)
    strategies_changed    = pyqtSignal()
    positions_updated     = pyqtSignal()

ui_signals = UISignals()



# ─────────────────────────────────────────────
# Виджет ячейки "Агент" (имя + ▶ ■)
# ─────────────────────────────────────────────
class AgentCellWidget(QWidget):
    def __init__(self, name: str, sid: str,
                 on_start, on_stop, is_active: bool):
        super().__init__()
        self.setAutoFillBackground(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 4, 0)
        layout.setSpacing(4)

        lbl = QLabel(name)
        lbl.setStyleSheet("color: #cdd6f4; background: transparent;")
        lbl.setToolTip(name)
        layout.addWidget(lbl, stretch=1)

        # Запуск
        btn_start = QPushButton()
        btn_start.setFixedSize(32, 28)
        btn_start.setToolTip("Запустить агента")
        btn_start.setStyleSheet("""
            QPushButton {
                background: #a6e3a1; color: #1e1e2e;
                border: none; border-radius: 3px;
                font-size: 11px; font-weight: bold;
            }
            QPushButton:hover   { background: #8ed490; }
            QPushButton:pressed { background: #72c07a; }
            QPushButton:disabled { background: #3a3a4a; color: #6c7086; }
        """)
        apply_icon(btn_start, 'actions/play.svg', 16)
        btn_start.setEnabled(not is_active)
        btn_start.clicked.connect(lambda: on_start(sid))
        layout.addWidget(btn_start)

        # Стоп
        btn_stop = QPushButton()
        btn_stop.setFixedSize(32, 28)
        btn_stop.setToolTip("Остановить агента")
        btn_stop.setStyleSheet("""
            QPushButton {
                background: #f38ba8; color: #1e1e2e;
                border: none; border-radius: 3px;
                font-size: 11px; font-weight: bold;
            }
            QPushButton:hover   { background: #e07090; }
            QPushButton:pressed { background: #c05070; }
            QPushButton:disabled { background: #3a3a4a; color: #6c7086; }
        """)
        apply_icon(btn_stop, 'actions/stop.svg', 16)
        btn_stop.setEnabled(is_active)
        btn_stop.clicked.connect(lambda: on_stop(sid))
        layout.addWidget(btn_stop)



class StatusCellWidget(QWidget):
    def __init__(self, text: str, color: str):
        super().__init__()
        self.setAutoFillBackground(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        dot = QFrame()
        dot.setFixedSize(10, 10)
        dot.setStyleSheet(
            f'background: {color}; border: none; border-radius: 5px;'
        )
        layout.addWidget(dot)

        lbl = QLabel(text)
        lbl.setStyleSheet(f'color: {color}; background: transparent;')
        layout.addWidget(lbl)

        self.setLayout(layout)


# ─────────────────────────────────────────────
# Виджет ячейки "Тикер" с кнопкой разворачивания
# ─────────────────────────────────────────────
class TickerExpandWidget(QWidget):
    """Ячейка тикера с кнопкой +/- для разворачивания списка инструментов.

    Используется только для стратегий с корзиной инструментов (instruments).
    При клике вызывает callback toggle_fn(sid).
    """
    def __init__(self, ticker_text: str, sid: str, expanded: bool, toggle_fn):
        super().__init__()
        self.setAutoFillBackground(False)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)

        self._btn = QPushButton()
        self._btn.setFixedSize(22, 22)
        self._btn.setToolTip("Развернуть/свернуть инструменты")
        self._btn.setStyleSheet("""
            QPushButton {
                background: #fab387; color: #1e1e2e;
                border: none; border-radius: 3px;
                font-size: 14px; font-weight: bold;
            }
            QPushButton:hover  { background: #f9a070; }
            QPushButton:pressed{ background: #e8905a; }
        """)
        self.set_expanded(expanded)
        self._btn.clicked.connect(lambda: toggle_fn(sid))
        layout.addWidget(self._btn)

        lbl = QLabel(ticker_text)
        lbl.setStyleSheet("color: #89b4fa; background: transparent;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl, stretch=1)

    def set_expanded(self, expanded: bool):
        apply_icon(self._btn, 'actions/collapse.svg' if expanded else 'actions/expand.svg', 14)


# ─────────────────────────────────────────────
# Таблица агентов с поддержкой drag-and-drop строк
# ─────────────────────────────────────────────
class AgentTable(QTableWidget):
    """QTableWidget с ручным drag-and-drop строк.

    InternalMove не используется — он перемещает только QTableWidgetItem,
    но не setCellWidget-виджеты, что ломает отображение.
    Вместо этого: зажимаем ЛКМ на строке → тянем → отпускаем →
    эмитируем row_order_changed(sids) с новым порядком sid.
    MainWindow перехватывает сигнал, обновляет self._row_order и вызывает
    _refresh_table — таблица полностью перестраивается в правильном порядке.
    """
    row_order_changed = pyqtSignal(list)  # список sid в новом порядке

    _DRAG_THRESHOLD = 8  # пикселей до начала drag

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._drag_start_row: int = -1
        self._drag_active: bool = False
        self._drag_start_y: int = 0
        self._drop_indicator_row: int = -1
        self.setMouseTracking(True)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            idx = self.indexAt(event.pos())
            if idx.isValid():
                self._drag_start_row = idx.row()
                self._drag_start_y = event.pos().y()
                self._drag_active = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._drag_start_row >= 0
                and event.buttons() & Qt.MouseButton.LeftButton):
            dy = abs(event.pos().y() - self._drag_start_y)
            if dy > self._DRAG_THRESHOLD:
                self._drag_active = True
            if self._drag_active:
                # Подсвечиваем строку-цель
                idx = self.indexAt(event.pos())
                new_indicator = idx.row() if idx.isValid() else self._drop_indicator_row
                if new_indicator != self._drop_indicator_row:
                    self._drop_indicator_row = new_indicator
                    self.viewport().update()
                return  # не передаём в super — блокируем выделение
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._drag_active:
            idx = self.indexAt(event.pos())
            target_row = idx.row() if idx.isValid() else -1
            src = self._drag_start_row
            if target_row >= 0 and target_row != src:
                self._emit_reordered(src, target_row)
            self._drag_start_row = -1
            self._drag_active = False
            self._drop_indicator_row = -1
            self.viewport().update()
            return
        self._drag_start_row = -1
        self._drag_active = False
        self._drop_indicator_row = -1
        super().mouseReleaseEvent(event)

    def viewportEvent(self, event):
        return super().viewportEvent(event)

    def _update_drag_indicator(self):
        """Перерисовывает viewport для отображения индикатора drag."""
        self.viewport().update()

    def _emit_reordered(self, src_row: int, dst_row: int):
        """Читает текущий порядок sid из items, переставляет src→dst, эмитирует сигнал."""
        # Собираем только родительские sid в текущем порядке строк
        sids = []
        seen = set()
        for row in range(self.rowCount()):
            item = self.item(row, 0)
            if item:
                sid = item.data(Qt.ItemDataRole.UserRole)
                is_child = item.data(Qt.ItemDataRole.UserRole + 1) is not None
                if sid and not is_child and sid not in seen:
                    sids.append(sid)
                    seen.add(sid)
        if not sids:
            return
        # Определяем sid перетаскиваемой строки
        drag_item = self.item(src_row, 0)
        if not drag_item:
            return
        drag_sid = drag_item.data(Qt.ItemDataRole.UserRole)
        if not drag_sid or drag_sid not in sids:
            return
        # Определяем sid строки-цели (ближайший родитель)
        drop_item = self.item(dst_row, 0)
        if not drop_item:
            return
        drop_sid = drop_item.data(Qt.ItemDataRole.UserRole)
        # Если drop на дочернюю строку — берём её родительский sid
        is_child = drop_item.data(Qt.ItemDataRole.UserRole + 1) is not None
        if is_child or drop_sid not in sids:
            # Ищем ближайший родительский sid выше dst_row
            for r in range(dst_row, -1, -1):
                it = self.item(r, 0)
                if it:
                    s = it.data(Qt.ItemDataRole.UserRole)
                    if s and it.data(Qt.ItemDataRole.UserRole + 1) is None and s in sids:
                        drop_sid = s
                        break
            else:
                return
        if drag_sid == drop_sid:
            return
        # Переставляем
        sids.remove(drag_sid)
        insert_at = sids.index(drop_sid)
        if dst_row > src_row:
            insert_at += 1
        sids.insert(insert_at, drag_sid)
        self.row_order_changed.emit(sids)


# ─────────────────────────────────────────────
# Главное окно
# ─────────────────────────────────────────────
class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.setMinimumSize(1000, 600)
        self.resize(1400, 820)
        self._log_visible = True
        self._log_views: dict[str, QTextEdit] = {}
        self._expanded: set = set()  # sid стратегий с развёрнутым списком инструментов
        self._row_order: list = []   # порядок sid агентов (in-memory, синхронизируется с QSettings)

        # Кэш для sec_info (чтобы не блокировать GUI при запросах к QUIK)
        self._sec_info_cache: dict = {}  # {(connector_id, ticker, board): (sec_info, timestamp)}
        self._refreshing_sec_info: set = set()  # множество ключей, которые уже загружаются
        self._SEC_INFO_TTL = 60  # обновлять раз в 60 секунд

        self._setup_core()
        self._build_ui()
        self._setup_shortcuts()
        self._connect_signals()
        self._start_timers()
        self._row_order = self._restore_row_order()  # загружаем сохранённый порядок до первого рендера
        self._refresh_table()
        self._restore_column_state()
        self.showMaximized()

        from ui.tray import TrayIcon
        self._tray = TrayIcon(self)
        ui_signals.connector_changed.connect(self._tray.set_connected)
        
        # Регистрация коннекторов после инициализации UI
        from core.connector_manager import register_connectors
        register_connectors()
        
        self._paper_mode = False  # отключён

    # ─────────────────────────────────────────────
    # Backend
    # ─────────────────────────────────────────────

    def _setup_core(self):
        notifier.load_from_settings()

        strategy_scheduler.start()

        # Финам
        finam_connector.subscribe_connect(lambda: ui_signals.connector_changed.emit("finam", True))
        finam_connector.subscribe_disconnect(lambda: ui_signals.connector_changed.emit("finam", False))
        finam_connector.subscribe_error(lambda m: ui_signals.log_message.emit(f"[Финам] Ошибка: {m}", "error"))
        finam_connector.subscribe_positions(lambda: ui_signals.positions_updated.emit())

        # QUIK
        quik_connector.subscribe_connect(lambda: ui_signals.connector_changed.emit("quik", True))
        quik_connector.subscribe_disconnect(lambda: ui_signals.connector_changed.emit("quik", False))
        quik_connector.subscribe_error(lambda m: ui_signals.log_message.emit(f"[QUIK] Ошибка: {m}", "error"))
        quik_connector.subscribe_positions(lambda: ui_signals.positions_updated.emit())

        logger.add(self._handle_log, level="DEBUG", format="{message}")
        self._preload_strategies()

    def _preload_strategies(self):
        for sid, data in get_all_strategies().items():
            fp = data.get("file_path", "")
            if not fp or strategy_loader.is_loaded(sid):
                continue
            try:
                strategy_loader.load(sid, fp)
                logger.debug(f"Предзагрузка стратегии: [{sid}]")
            except StrategyLoadError as e:
                logger.warning(f"Не удалось предзагрузить [{sid}]: {e}")

    # ─────────────────────────────────────────────
    # UI
    # ─────────────────────────────────────────────

    def _build_ui(self):
        # ── Корневой виджет с вертикальным layout ──
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ── Топ-бар (над вкладками, всегда виден) ──
        root_layout.addWidget(self._build_topbar())

        # ── QTabWidget — браузерные вкладки ──
        self._tabs = QTabWidget()
        self._tabs.setTabsClosable(False)   # по умолчанию — без крестиков
        self._tabs.setMovable(True)
        self._tabs.setDocumentMode(True)    # стиль «браузер» без рамки
        root_layout.addWidget(self._tabs, stretch=1)

        # ── Вкладка 0: Главная ──
        home_widget = QWidget()
        home_widget.setStyleSheet("QWidget { background-color: #252535; }")
        home_layout = QVBoxLayout(home_widget)
        home_layout.setContentsMargins(0, 0, 0, 0)
        home_layout.setSpacing(0)
        home_layout.addWidget(self._build_toolbar())
        home_layout.addWidget(self._build_table(), stretch=1)
        home_layout.addWidget(self._build_logbar())
        self._tabs.addTab(home_widget, "🏠  Главная")

        # ── Вкладка 1: Настройки ──
        self._settings_tab_widget = self._build_settings_tab()
        self._tabs.addTab(self._settings_tab_widget, "⚙  Настройки")

        # Индекс вкладки настроек — фиксированный (1)
        self._TAB_HOME     = 0
        self._TAB_SETTINGS = 1

        # Словарь: ключ → индекс вкладки графика
        self._chart_tabs: dict[str, int] = {}

        # Закрытие вкладок (только для графиков)
        self._tabs.tabCloseRequested.connect(self._close_tab)

        self._build_statusbar()

    # ── Топ-бар ────────────────────────────────

    def _build_topbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(52)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 6, 16, 6)
        layout.setSpacing(10)

        title = QLabel(f"📈  {APP_NAME}")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #89b4fa;")
        layout.addWidget(title)
        layout.addStretch()

        # ── Блок Финам ──────────────────────────────
        finam_block = self._build_connector_block(
            connector_id="finam",
            label="Финам",
        )
        layout.addWidget(finam_block)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setStyleSheet("color: #313244;")
        layout.addWidget(separator)

        # ── Блок QUIK ───────────────────────────────
        quik_block = self._build_connector_block(
            connector_id="quik",
            label="QUIK",
        )
        layout.addWidget(quik_block)

        return bar

    def _build_connector_block(self, connector_id: str, label: str) -> QWidget:
        """Блок статуса + кнопки для одного коннектора."""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(6)

        lbl = QLabel(f"⚫  {label}: отключён")
        lbl.setStyleSheet("color: #f38ba8; font-weight: bold; min-width: 160px;")
        lbl.setObjectName(f"lbl_connector_{connector_id}")
        layout.addWidget(lbl)

        btn_conn = QPushButton("Подключить")
        btn_conn.setObjectName("btn_connect")
        btn_conn.setMinimumWidth(110)
        btn_conn.setToolTip(f"Подключить {label}")
        btn_conn.clicked.connect(lambda: self._on_connect(connector_id))
        layout.addWidget(btn_conn)

        btn_disc = QPushButton("Отключить")
        btn_disc.setObjectName("btn_disconnect")
        btn_disc.setMinimumWidth(100)
        btn_disc.setToolTip(f"Отключить {label}")
        btn_disc.clicked.connect(lambda: self._on_disconnect(connector_id))
        layout.addWidget(btn_disc)

        # Сохраняем ссылку на лейбл для обновления
        setattr(self, f"_lbl_{connector_id}", lbl)

        return widget

    # ── Тулбар ─────────────────────────────────

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(40)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(6)

        _SS_ADD = """
            QPushButton { background:#1e90ff; color:#ffffff; border:none; border-radius:5px; padding:6px 14px; font-size:13px; font-weight:bold; }
            QPushButton:hover   { background:#1a7de0; }
            QPushButton:pressed { background:#1570c8; }
        """
        _SS_START = """
            QPushButton { background:#a6e3a1; color:#1e1e2e; border:none; border-radius:5px; padding:6px 14px; font-size:13px; font-weight:bold; }
            QPushButton:hover   { background:#8ed490; }
            QPushButton:pressed { background:#72c07a; }
        """
        _SS_STOP = """
            QPushButton { background:#f38ba8; color:#1e1e2e; border:none; border-radius:5px; padding:6px 14px; font-size:13px; font-weight:bold; }
            QPushButton:hover   { background:#e07090; }
            QPushButton:pressed { background:#c05070; }
        """

        # Paper Mode переключатель
        btn_add = QPushButton("+ Добавить агента")
        btn_add.setObjectName("btn_add")
        btn_add.setToolTip("Добавить нового агента (Ctrl+N)")
        btn_add.setStyleSheet(_SS_ADD)
        btn_add.clicked.connect(self._add_strategy)
        layout.addWidget(btn_add)

        self.btn_start_all = QPushButton("▶ Запустить все")
        self.btn_start_all.setObjectName("btn_start")
        self.btn_start_all.setToolTip("Запустить всех агентов")
        self.btn_start_all.setStyleSheet(_SS_START)
        self.btn_start_all.clicked.connect(self._start_all)
        layout.addWidget(self.btn_start_all)

        self.btn_stop_all = QPushButton("■ Остановить все")
        self.btn_stop_all.setObjectName("btn_stop")
        self.btn_stop_all.setToolTip("Остановить всех агентов")
        self.btn_stop_all.setStyleSheet(_SS_STOP)
        self.btn_stop_all.clicked.connect(self._stop_all)
        layout.addWidget(self.btn_stop_all)

        layout.addSpacing(8)

        self.btn_start_sel = QPushButton("▶ Запустить выбранные")
        self.btn_start_sel.setObjectName("btn_start")
        self.btn_start_sel.setToolTip("Запустить выбранных агентов")
        self.btn_start_sel.setStyleSheet(_SS_START)
        self.btn_start_sel.clicked.connect(self._start_selected)
        layout.addWidget(self.btn_start_sel)

        self.btn_stop_sel = QPushButton("■ Остановить выбранные")
        self.btn_stop_sel.setObjectName("btn_stop")
        self.btn_stop_sel.setToolTip("Остановить выбранных агентов")
        self.btn_stop_sel.setStyleSheet(_SS_STOP)
        self.btn_stop_sel.clicked.connect(self._stop_selected)
        layout.addWidget(self.btn_stop_sel)

        layout.addStretch()

        self.btn_remove = QPushButton("Удалить агента")
        self.btn_remove.setToolTip("Удалить выбранных агентов (Delete)")
        self.btn_remove.clicked.connect(self._remove_strategy)
        layout.addWidget(self.btn_remove)

        return bar

    # ── Таблица ────────────────────────────────

    def _build_table(self) -> QWidget:
        self.table = AgentTable()
        self.table.setColumnCount(len(COLUMNS))
        self.table.setHorizontalHeaderLabels([c[0] for c in COLUMNS])

        hh = self.table.horizontalHeader()

        # ── Все колонки — Interactive (ручной resize)
        for i in range(len(COLUMNS)):
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(i, COLUMNS[i][1])

        # Только иконка чарта — фиксированная
        hh.setSectionResizeMode(
            COL["📈"], QHeaderView.ResizeMode.Fixed
        )
        self.table.setColumnWidth(COL["📈"], 44)

        # ── Перетаскивание колонок
        hh.setSectionsMovable(True)
        hh.setDragEnabled(True)

        # ── Горизонтальный скролл если не влезает
        hh.setStretchLastSection(False)
        self.table.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )

        self.table.verticalHeader().setVisible(False)
        # Drag строк реализован вручную в AgentTable (mouse events).
        # InternalMove не используется — он не переносит setCellWidget-виджеты.
        self.table.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.setAlternatingRowColors(True)
        self.table.setStyleSheet(
            self.table.styleSheet() +
            "QTableWidget { alternate-background-color: #1e1e30; }"
        )
        self.table.doubleClicked.connect(self._on_row_double_click)
        # Сигнал от AgentTable — порядок строк изменился после drop
        self.table.row_order_changed.connect(self._on_row_order_changed)

        # Подсказка по управлению колонками
        hh.setToolTip(
            "Перетащи заголовок — изменить порядок колонок\n"
            "Потяни границу — изменить ширину"
        )

        return self.table

    # ── Лог-панель ─────────────────────────────

    def _build_logbar(self) -> QFrame:
        self.logbar = QFrame()
        self.logbar.setObjectName("logbar")
        self.logbar.setFixedHeight(160)
        layout = QVBoxLayout(self.logbar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(24)
        header.setStyleSheet(
            "background-color: #181825; border-top: 1px solid #313244;"
        )
        hl = QHBoxLayout(header)
        hl.setContentsMargins(12, 2, 12, 2)

        lbl = QLabel("▼  Лог событий")
        lbl.setStyleSheet("color: #6c7086; font-size: 11px;")
        hl.addWidget(lbl)
        hl.addStretch()

        self.btn_log_toggle = QPushButton("Свернуть")
        self.btn_log_toggle.setObjectName("btn_log_toggle")
        self.btn_log_toggle.setFixedWidth(70)
        self.btn_log_toggle.clicked.connect(self._toggle_log)
        hl.addWidget(self.btn_log_toggle)

        btn_clear = QPushButton("Очистить")
        btn_clear.setObjectName("btn_log_toggle")
        btn_clear.setFixedWidth(70)
        btn_clear.clicked.connect(self._clear_logs)
        hl.addWidget(btn_clear)

        layout.addWidget(header)

        self.log_tabs = QTabWidget()
        self.log_tabs.setDocumentMode(True)
        self.log_tabs.setTabPosition(QTabWidget.TabPosition.North)

        for tab_name in ('Общий', 'Соединение', 'Позиции', 'Дебаг', 'Ошибки'):
            view = QTextEdit()
            view.setReadOnly(True)
            view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
            self._log_views[tab_name] = view
            self.log_tabs.addTab(view, tab_name)

        self.log_view = self._log_views['Общий']
        layout.addWidget(self.log_tabs)

        return self.logbar

    def _build_statusbar(self):
        bar = QStatusBar()
        self.setStatusBar(bar)
        self.lbl_time       = QLabel()
        self.lbl_strategies = QLabel()
        self.lbl_connectors = QLabel()
        self.lbl_next_event = QLabel()
        bar.addWidget(self.lbl_time)
        bar.addWidget(self.lbl_strategies)
        bar.addWidget(self.lbl_connectors)
        bar.addPermanentWidget(self.lbl_next_event)

    # ─────────────────────────────────────────────
    # Таблица — заполнение
    # ─────────────────────────────────────────────

    def _toggle_instruments(self, sid: str):
        """Переключает развёрнутость списка инструментов стратегии."""
        if sid in self._expanded:
            self._expanded.discard(sid)
        else:
            self._expanded.add(sid)
        self._refresh_table()

    # ─────────────────────────────────────────────
    # Кэширование sec_info (защита от блокировки GUI)
    # ─────────────────────────────────────────────

    def _get_sec_info_cached(self, connector, connector_id: str, ticker: str, board: str):
        """Возвращает sec_info из кэша или None, не блокируя GUI."""
        import time
        key = (connector_id, ticker, board)
        cached = self._sec_info_cache.get(key)
        
        if cached:
            sec_info, ts = cached
            if time.monotonic() - ts < self._SEC_INFO_TTL:
                return sec_info  # отдаём из кэша
        
        # Кэш устарел — обновляем в фоне, возвращаем старое значение если есть
        if cached:
            self._refresh_sec_info_background(connector, connector_id, ticker, board)
            return cached[0]
        
        # Первый запрос — тоже в фоне
        self._refresh_sec_info_background(connector, connector_id, ticker, board)
        return None

    def _refresh_sec_info_background(self, connector, connector_id: str, ticker: str, board: str):
        """Обновляет sec_info в фоновом потоке.
        
        Использует _refreshing_sec_info для предотвращения дублирующих потоков.
        """
        import threading
        import time
        key = (connector_id, ticker, board)
        
        # Проверяем, не запущен ли уже поток для этого ключа
        if key in self._refreshing_sec_info:
            return  # уже в процессе
        
        self._refreshing_sec_info.add(key)
        
        def _fetch():
            try:
                sec_info = connector.get_sec_info(ticker, board)
                if sec_info:
                    self._sec_info_cache[key] = (sec_info, time.monotonic())
            except Exception:
                pass
            finally:
                self._refreshing_sec_info.discard(key)
        
        threading.Thread(target=_fetch, daemon=True).start()

    def _refresh_table(self):
        from core.autostart import get_live_engines
        from core.connector_manager import connector_manager

        strategies = get_all_strategies()
        engines = get_live_engines()

        # Применяем сохранённый порядок строк (из in-memory кэша, не из QSettings)
        saved_order = self._row_order
        if saved_order:
            ordered = [(sid, strategies[sid]) for sid in saved_order if sid in strategies]
            ordered += [(sid, data) for sid, data in strategies.items() if sid not in saved_order]
        else:
            ordered = list(strategies.items())

        # Подсчитываем общее кол-во строк (стратегии + дочерние инструменты)
        total_rows = 0
        for sid, data in ordered:
            total_rows += 1
            params = data.get("params", {})
            instruments = params.get("instruments", [])
            if instruments and sid in self._expanded:
                total_rows += len(instruments)

        self.table.setRowCount(total_rows)

        row = 0
        for sid, data in ordered:
            self.table.setRowHeight(row, 32)

            status = data.get("status", "stopped")
            name = data.get("name", sid)
            params = data.get("params", {})
            ticker = data.get("ticker") or params.get("ticker", "—")
            account = data.get("finam_account", "—") or "—"
            aliases = get_setting("account_aliases") or {}
            account_label = aliases.get(account, account)

            # Инструменты корзины (для Ахиллес и подобных)
            instruments: list = params.get("instruments", [])
            has_instruments = bool(instruments)

            # Позиции — берём из LiveEngine если доступен, иначе из коннектора
            engine = engines.get(sid)

            if engine and engine.is_running:
                pos_info = engine.get_position_info()
                pos_qty = str(pos_info["quantity"])
                avg_price = f"{pos_info['avg_price']:.3f}" if pos_info["avg_price"] != 0 else "—"
                cur_price = f"{pos_info['current_price']:.3f}" if pos_info["current_price"] != 0 else "—"
                pnl = pos_info["pnl"] if pos_info["quantity"] else None
            else:
                connector_id = data.get("connector", "finam")
                connector = connector_manager.get(connector_id)
                positions = (
                    connector.get_positions(account)
                    if connector and connector.is_connected() and account != "—"
                    else []
                )
                pos = next(
                    (p for p in positions if p.get("ticker") == ticker), None
                )
                pos_qty = str(int(pos["quantity"])) if pos else "0"
                avg_price = f"{pos['avg_price']:.3f}" if pos else "—"
                cur_price = f"{pos['current_price']:.3f}" if pos else "—"
                pnl = pos["pnl"] if pos else None

            # Нарастающий итог П/У и комиссия — общие по стратегии
            total_pnl = get_total_pnl(sid)
            open_commission = get_open_commission(sid)
            total_commission = get_total_commission(sid)

            # Статус
            status_map = {
                "active": ("Активен", "#a6e3a1"),
                "stopped": ("Остановлен", "#f38ba8"),
                "waiting": ("Ожидание", "#f9e2af"),
                "error": ("Ошибка", "#fab387"),
            }
            status_text, status_color = status_map.get(
                status, ("Неизвестно", "#6c7086")
            )

            center = Qt.AlignmentFlag.AlignCenter
            left = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft

            def cell(text, color=None, align=left, _sid=sid):
                item = QTableWidgetItem(str(text))
                item.setTextAlignment(align)
                if color:
                    item.setForeground(QColor(color))
                item.setData(Qt.ItemDataRole.UserRole, _sid)
                return item

            # Колонка «Агент» — виджет с кнопками
            is_active = status == "active"
            agent_widget = AgentCellWidget(
                name=name, sid=sid,
                on_start=self._start_agent,
                on_stop=self._stop_agent,
                is_active=is_active,
            )
            dummy = QTableWidgetItem()
            dummy.setData(Qt.ItemDataRole.UserRole, sid)
            self.table.setItem(row, COL["Агент"], dummy)
            self.table.setCellWidget(row, COL["Агент"], agent_widget)

            # Колонка «Тикер» — с кнопкой разворачивания если есть корзина
            if has_instruments:
                expanded = sid in self._expanded
                ticker_label = f"{len(instruments)} инстр."
                tw = TickerExpandWidget(ticker_label, sid, expanded, self._toggle_instruments)
                dummy_t = QTableWidgetItem()
                dummy_t.setData(Qt.ItemDataRole.UserRole, sid)
                self.table.setItem(row, COL["Тикер"], dummy_t)
                self.table.setCellWidget(row, COL["Тикер"], tw)
            else:
                self.table.setItem(row, COL["Тикер"],
                                   cell(ticker, "#89b4fa", center))

            self.table.setItem(row, COL["Счёт"],
                               cell(account_label, "#6c7086", center))
            status_dummy = QTableWidgetItem()
            status_dummy.setData(Qt.ItemDataRole.UserRole, sid)
            self.table.setItem(row, COL["Состояние"], status_dummy)
            self.table.setCellWidget(row, COL["Состояние"], StatusCellWidget(status_text, status_color))
            self.table.setItem(row, COL["Позиция"],
                               cell(pos_qty, "#cdd6f4", center))

            # П.Лот — потенциальный лот (только при позиции 0)
            pot_lot_str = ""
            try:
                pos_qty_num = float(pos_qty)
            except (ValueError, TypeError):
                pos_qty_num = 1
            if pos_qty_num == 0:
                lot_sizing = data.get("lot_sizing", {})
                connector_id = data.get("connector", "finam")
                connector = connector_manager.get(connector_id)
                board = data.get("board", "FUT")
                if lot_sizing.get("dynamic") and connector and connector.is_connected():
                    from core.equity_tracker import get_max_drawdown
                    import math
                    free_money = connector.get_free_money(account)
                    sec_info = self._get_sec_info_cached(connector, connector_id, ticker, board) if hasattr(connector, "get_sec_info") else None
                    go = 0.0
                    if sec_info:
                        go = float(sec_info.get("buy_deposit") or sec_info.get("sell_deposit") or 0)
                    if free_money and free_money > 0:
                        if go <= 0:
                            pot_lot_str = str(int(lot_sizing.get("lot", 1)) or 1)
                        else:
                            dd = float(lot_sizing.get("drawdown") or 0) or (get_max_drawdown(sid) or 0)
                            instances = max(int(lot_sizing.get("instances", 1)), 1)
                            denom = dd + go
                            dyn = math.floor((free_money / denom) / instances) if denom > 0 else 0
                            # dyn >= 1: можем купить хотя бы 1 лот → показываем dyn
                            # dyn < 1: денег не хватает → показываем "0"
                            pot_lot_str = str(dyn) if dyn >= 1 else "0"
                    else:
                        pot_lot_str = "—"
                else:
                    static_qty = int(lot_sizing.get("lot", data.get("params", {}).get("qty", 1)))
                    can_afford = True
                    if connector and connector.is_connected():
                        free_money = connector.get_free_money(account)
                        sec_info = self._get_sec_info_cached(connector, connector_id, ticker, data.get("board", "FUT"))
                        if free_money is not None and sec_info:
                            from core.equity_tracker import get_max_drawdown
                            go = sec_info.get("buy_deposit") or sec_info.get("sell_deposit") or 0
                            dd = lot_sizing.get("drawdown", 0) or get_max_drawdown(sid) or 0
                            can_afford = free_money >= (go + dd)
                    pot_lot_str = str(static_qty) if can_afford else "0"
            self.table.setItem(row, COL["П.Лот"],
                               cell(pot_lot_str, "#89dceb", center))
            self.table.setItem(row, COL["Уч. цена"],
                               cell(avg_price, "#cdd6f4", center))
            self.table.setItem(row, COL["Текущая"],
                               cell(cur_price, "#cdd6f4", center))

            # П/У текущей позиции
            if pnl is not None:
                pnl_color = "#a6e3a1" if pnl >= 0 else "#f38ba8"
                pnl_str = f"{'+' if pnl >= 0 else ''}{pnl:.2f}"
            else:
                pnl_color, pnl_str = "#6c7086", "—"
            self.table.setItem(row, COL["П/У"],
                               cell(pnl_str, pnl_color, center))

            # Комиссия текущей позиции — уже уплаченная комиссия по открытому остатку
            if open_commission is not None:
                comm_color, comm_str = "#f9e2af", f"{open_commission:.2f}"
            else:
                comm_color, comm_str = "#6c7086", "—"
            self.table.setItem(row, COL["Комиссия"],
                               cell(comm_str, comm_color, center))

            # Итог П/У — общий по стратегии
            if total_pnl is not None:
                tc = "#a6e3a1" if total_pnl >= 0 else "#f38ba8"
                t_str = f"{'+' if total_pnl >= 0 else ''}{total_pnl:.2f}"
            else:
                tc, t_str = "#6c7086", "—"
            self.table.setItem(row, COL["Итог П/У"],
                               cell(t_str, tc, center))

            # Накопленная комиссия по агенту
            total_commission_str = f"{total_commission:.2f}"
            self.table.setItem(row, COL["Итого комиссия"],
                               cell(total_commission_str, "#f9e2af", center))

            # Кнопка истории ордеров
            btn_history = QPushButton()
            btn_history.setFixedSize(36, 26)
            btn_history.setToolTip(f"История ордеров {name}")
            btn_history.setStyleSheet("""
                QPushButton {
                    background: #f9e2af; border: none;
                    border-radius: 4px; font-size: 14px;
                }
                QPushButton:hover { background: #f9e2af; color: #1e1e2e; }
            """)
            apply_icon(btn_history, 'actions/history.svg', 16)
            btn_history.clicked.connect(lambda _, s=sid: self._open_order_history(s))
            self.table.setCellWidget(row, COL["История ордеров"], btn_history)

            # Кнопка чарта
            btn_chart = QPushButton()
            btn_chart.setFixedSize(36, 26)
            btn_chart.setToolTip(f"График {ticker}")
            btn_chart.setStyleSheet("""
                QPushButton {
                    background: #89b4fa; border: none;
                    border-radius: 4px; font-size: 14px;
                }
                QPushButton:hover { background: #89b4fa; }
            """)
            apply_icon(btn_chart, 'actions/chart.svg', 16)
            btn_chart.clicked.connect(lambda _, s=sid: self._open_chart(s))
            self.table.setCellWidget(row, COL["📈"], btn_chart)

            row += 1

            # ── Дочерние строки инструментов ──────────
            if has_instruments and sid in self._expanded:
                pnl_by_ticker = get_pnl_by_ticker(sid)
                child_bg = QColor("#1a1a2e")

                for instr in instruments:
                    # instr может быть строкой или dict {"ticker": ..., "board": ...}
                    if isinstance(instr, dict):
                        t_ticker = instr.get("ticker", "?")
                        t_board = instr.get("board", "")
                    else:
                        t_ticker = str(instr)
                        t_board = ""

                    self.table.setRowHeight(row, 28)

                    def child_cell(text, color=None, align=center, _sid=sid):
                        item = QTableWidgetItem(str(text))
                        item.setTextAlignment(align)
                        item.setBackground(child_bg)
                        if color:
                            item.setForeground(QColor(color))
                        item.setData(Qt.ItemDataRole.UserRole, _sid)
                        # Помечаем как дочернюю строку
                        item.setData(Qt.ItemDataRole.UserRole + 1, t_ticker)
                        return item

                    # Агент — отступ + имя тикера
                    self.table.setItem(row, COL["Агент"],
                                       child_cell(f"    └ {t_ticker}", "#6c7086", left))
                    # Тикер
                    board_label = f"{t_ticker}" + (f" [{t_board}]" if t_board else "")
                    self.table.setItem(row, COL["Тикер"],
                                       child_cell(board_label, "#89b4fa", center))
                    # Счёт, Состояние, Позиция, П.Лот, Уч.цена, Текущая, П/У, комиссии — пусто
                    for col_name in (
                        "Счёт", "Состояние", "Позиция", "П.Лот", "Уч. цена",
                        "Текущая", "П/У", "Комиссия", "Итого комиссия",
                    ):
                        self.table.setItem(row, COL[col_name], child_cell("—", "#45475a"))

                    # Итог П/У по тикеру
                    t_pnl = pnl_by_ticker.get(t_ticker)
                    if t_pnl is not None:
                        tp_color = "#a6e3a1" if t_pnl >= 0 else "#f38ba8"
                        tp_str = f"{'+' if t_pnl >= 0 else ''}{t_pnl:.2f}"
                    else:
                        tp_color, tp_str = "#45475a", "—"
                    self.table.setItem(row, COL["Итог П/У"],
                                       child_cell(tp_str, tp_color, center))

                    # Кнопка истории ордеров для инструмента
                    btn_child_history = QPushButton()
                    btn_child_history.setFixedSize(36, 26)
                    btn_child_history.setToolTip(f"История ордеров {t_ticker}")
                    btn_child_history.setStyleSheet("""
                        QPushButton {
                            background: #f9e2af; border: none;
                            border-radius: 4px; font-size: 14px;
                        }
                        QPushButton:hover { background: #f9e2af; color: #1e1e2e; }
                    """)
                    apply_icon(btn_child_history, 'actions/history.svg', 16)
                    btn_child_history.clicked.connect(
                        lambda _, s=sid, t=t_ticker: self._open_order_history(s, t)
                    )
                    self.table.setCellWidget(row, COL["История ордеров"], btn_child_history)

                    # Кнопка чарта для инструмента
                    btn_child_chart = QPushButton()
                    btn_child_chart.setFixedSize(36, 26)
                    btn_child_chart.setToolTip(f"График {t_ticker}")
                    btn_child_chart.setStyleSheet("""
                        QPushButton {
                            background: #89b4fa; border: none;
                            border-radius: 4px; font-size: 14px;
                        }
                        QPushButton:hover { background: #89b4fa; }
                    """)
                    apply_icon(btn_child_chart, 'actions/chart.svg', 16)
                    btn_child_chart.clicked.connect(
                        lambda _, s=sid, t=t_ticker, b=t_board: self._open_chart_instrument(s, t, b)
                    )
                    self.table.setCellWidget(row, COL["📈"], btn_child_chart)

                    row += 1

    def _open_backtest(self, strategy_id: str):
        from ui.backtest_window import BacktestWindow
        from core.storage import get_strategy

        data = get_strategy(strategy_id)
        file_path = data.get("file_path", "") if data else ""

        dlg = BacktestWindow(
            strategy_id=strategy_id,
            strategy_file_path=file_path,
            parent=self
        )
        dlg.exec()

    def _open_order_history(self, strategy_id: str, ticker: str | None = None):
        from core.storage import get_strategy
        from ui.order_history_window import OrderHistoryWindow

        data = get_strategy(strategy_id) or {}
        strategy_name = data.get('name', strategy_id)
        dlg = OrderHistoryWindow(
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            ticker=ticker,
            parent=self,
        )
        dlg.exec()

    # ─────────────────────────────────────────────
    # Вкладки графиков
    # ─────────────────────────────────────────────

    def _build_settings_tab(self) -> QWidget:
        """Создаёт виджет настроек для встраивания во вкладку QTabWidget.

        Использует SettingsWidget — QWidget-версию настроек без кнопки «Отмена».
        Все методы сохранения (_save_all, _tab_*) унаследованы через _SettingsMixin.
        """
        from ui.settings_window import SettingsWidget
        return SettingsWidget(parent=self)

    def _open_chart(self, strategy_id: str):
        """Открывает график агента во вкладке. Если вкладка уже есть — переключается на неё."""
        self._open_chart_tab(key=strategy_id, strategy_id=strategy_id)

    def _open_chart_instrument(self, strategy_id: str, ticker: str, board: str):
        """Открывает чарт для конкретного инструмента из корзины стратегии во вкладке."""
        key = f"{strategy_id}:{ticker}"
        self._open_chart_tab(
            key=key, strategy_id=strategy_id,
            ticker_override=ticker, board_override=board or None,
        )

    def _open_chart_tab(self, key: str, strategy_id: str,
                        ticker_override: str = None, board_override: str = None):
        """Универсальный метод открытия вкладки с графиком.

        key — уникальный ключ вкладки (sid или sid:ticker).
        Если вкладка с таким ключом уже открыта — просто переключается на неё.
        Иначе создаёт ChartWindow как QWidget и добавляет во вкладку.
        Вкладки графиков имеют кнопку закрытия (×).
        """
        from ui.chart_window import ChartWindow
        from core.storage import get_strategy

        # Если вкладка уже открыта — переключаемся
        if key in self._chart_tabs:
            idx = self._chart_tabs[key]
            if idx < self._tabs.count():
                self._tabs.setCurrentIndex(idx)
                return
            else:
                # Вкладка была закрыта — удаляем из словаря
                del self._chart_tabs[key]

        # Определяем имя вкладки
        data = get_strategy(strategy_id) or {}
        agent_name = data.get("name", strategy_id)
        if ticker_override:
            tab_label = f"📈 {ticker_override}"
        else:
            ticker = data.get("ticker") or data.get("params", {}).get("ticker", "")
            tab_label = f"📈 {agent_name}" + (f" ({ticker})" if ticker else "")

        # Создаём виджет графика
        chart = ChartWindow(
            strategy_id=strategy_id,
            parent=self,
            ticker_override=ticker_override,
            board_override=board_override,
        )

        # Добавляем вкладку
        idx = self._tabs.addTab(chart, tab_label)
        self._chart_tabs[key] = idx

        # Кастомная кнопка закрытия вкладки
        btn_close = QPushButton()
        btn_close.setFixedSize(18, 18)
        btn_close.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #f38ba8;
                border: none;
                border-radius: 3px;
                font-size: 14px;
                font-weight: bold;
                padding: 0;
            }
            QPushButton:hover {
                background: #f38ba8;
                color: #1e1e2e;
            }
        """)
        apply_icon(btn_close, 'actions/close.svg', 12)
        btn_close.setToolTip("Закрыть вкладку")
        btn_close.clicked.connect(lambda _, i=idx: self._close_tab_by_key(key))
        self._tabs.tabBar().setTabButton(idx, QTabBar.ButtonPosition.RightSide, btn_close)

        self._tabs.setCurrentIndex(idx)

    def _close_tab(self, index: int):
        """Закрывает вкладку по индексу (сигнал tabCloseRequested — не используется
        при кастомных кнопках, но оставлен для совместимости)."""
        if index in (self._TAB_HOME, self._TAB_SETTINGS):
            return
        # Находим ключ по индексу
        key = next((k for k, i in self._chart_tabs.items() if i == index), None)
        if key:
            self._close_tab_by_key(key)

    def _close_tab_by_key(self, key: str):
        """Закрывает вкладку графика по ключу (sid или sid:ticker).

        Используется кастомной красной кнопкой × на вкладке.
        Корректно обновляет индексы всех оставшихся вкладок.
        """
        if key not in self._chart_tabs:
            return
        index = self._chart_tabs[key]

        # Останавливаем таймеры/загрузчики графика перед удалением
        widget = self._tabs.widget(index)
        if widget and hasattr(widget, "closeEvent"):
            from PyQt6.QtGui import QCloseEvent
            widget.closeEvent(QCloseEvent())

        self._tabs.removeTab(index)

        # Обновляем индексы в словаре (после удаления индексы сдвигаются)
        updated: dict[str, int] = {}
        for k, idx in self._chart_tabs.items():
            if k == key:
                continue  # эта вкладка удалена
            elif idx > index:
                updated[k] = idx - 1
            else:
                updated[k] = idx
        self._chart_tabs = updated

    def _get_selected_ids(self) -> list[str]:
        rows = set(i.row() for i in self.table.selectedItems())
        result = []
        seen = set()
        for row in rows:
            # Пробуем получить item из любой колонки
            item = None
            for c in range(self.table.columnCount()):
                it = self.table.item(row, c)
                if it:
                    item = it
                    break
            if item:
                # Пропускаем дочерние строки инструментов
                if item.data(Qt.ItemDataRole.UserRole + 1) is not None:
                    continue
                sid = item.data(Qt.ItemDataRole.UserRole)
                if sid and sid not in seen:
                    result.append(sid)
                    seen.add(sid)
        return result

    # ─────────────────────────────────────────────
    # Действия с агентами
    # ─────────────────────────────────────────────

    def _start_agent(self, sid: str):
        from core.autostart import start_live_engine
        from core.connector_manager import connector_manager

        data = get_strategy(sid)
        if data:
            connector_id = data.get("connector", "finam")
            connector = connector_manager.get(connector_id)
            if not connector or not connector.is_connected():
                self._log(
                    f"Агент [{data['name']}] не запущен: коннектор [{connector_id}] офлайн",
                    "error"
                )
                return

            data["status"] = "active"
            save_strategy(sid, data)
            if start_live_engine(sid, wait_for_connection=True):
                self._log(f"Агент [{data['name']}] запущен", "info")
            else:
                self._log(
                    f"Агент [{data['name']}] не запущен: коннектор офлайн",
                    "error"
                )
                data["status"] = "stopped"
                save_strategy(sid, data)
            ui_signals.strategies_changed.emit()

    def _stop_agent(self, sid: str):
        # Останавливаем LiveEngine если запущен
        from core.autostart import stop_live_engine
        stop_live_engine(sid)
        
        data = get_strategy(sid)
        if data:
            data["status"] = "stopped"
            save_strategy(sid, data)
            self._log(f"Агент [{data['name']}] остановлен", "info")
            ui_signals.strategies_changed.emit()

    def _add_strategy(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Выбери файл стратегии",
            "strategies/", "Python файлы (*.py)"
        )
        if not path:
            return
        try:
            tmp_id = path.replace("\\", "/").split("/")[-1].replace(".py", "")
            loaded = strategy_loader.load(tmp_id, path)
            save_strategy(tmp_id, {
                "name":          loaded.info.get("name", tmp_id),
                "file_path":     path,
                "description":   loaded.info.get("description", ""),
                "status":        "stopped",
                "finam_account": "",
                "is_enabled":    True,
                "params":        {
                    k: v["default"]
                    for k, v in loaded.params_schema.items()
                },
            })
            self._log(f"Агент добавлен: {loaded.info.get('name')}", "info")
            ui_signals.strategies_changed.emit()
        except StrategyLoadError as e:
            QMessageBox.critical(self, "Ошибка загрузки", str(e))

    def _start_all(self):
        from core.autostart import start_live_engine

        for sid, data in get_all_strategies().items():
            data["status"] = "active"
            save_strategy(sid, data)
            start_live_engine(sid, wait_for_connection=False)
        self._log("Все агенты запущены", "info")
        ui_signals.strategies_changed.emit()

    def _stop_all(self):
        from core.autostart import stop_live_engine

        for sid, data in get_all_strategies().items():
            stop_live_engine(sid)
            data["status"] = "stopped"
            save_strategy(sid, data)
        self._log("Все агенты остановлены", "info")
        ui_signals.strategies_changed.emit()

    def _start_selected(self):
        from core.autostart import start_live_engine

        for sid in self._get_selected_ids():
            data = get_strategy(sid)
            if data:
                data["status"] = "active"
                save_strategy(sid, data)
                if start_live_engine(sid, wait_for_connection=False):
                    self._log(f"Агент [{data['name']}] запущен", "info")
                else:
                    self._log(
                        f"Агент [{data['name']}] помечен active, но LiveEngine не запущен",
                        "warning"
                    )
        ui_signals.strategies_changed.emit()

    def _stop_selected(self):
        from core.autostart import stop_live_engine

        for sid in self._get_selected_ids():
            data = get_strategy(sid)
            if data:
                stop_live_engine(sid)
                data["status"] = "stopped"
                save_strategy(sid, data)
                self._log(f"Агент [{data['name']}] остановлен", "info")
        ui_signals.strategies_changed.emit()

    def _remove_strategy(self):
        ids = self._get_selected_ids()
        if not ids:
            return
        names = [
            get_strategy(sid)["name"]
            for sid in ids if get_strategy(sid)
        ]
        reply = QMessageBox.question(
            self, "Удалить агентов",
            f"Удалить: {', '.join(names)}?\n\n"
            f"История ордеров и накопленный П/У будут обнулены.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            for sid in ids:
                clear_orders(sid)          # ← обнуляем историю и итог П/У
                delete_strategy(sid)
                strategy_loader.unload(sid)
            self._log(f"Удалено агентов: {len(ids)}", "info")
            ui_signals.strategies_changed.emit()

    def _on_row_double_click(self):
        ids = self._get_selected_ids()
        if not ids:
            return
        from ui.strategy_window import StrategyWindow
        self._strategy_win = StrategyWindow(ids[0], parent=self)
        self._strategy_win.show()

    # ─────────────────────────────────────────────
    # Коннектор
    # ─────────────────────────────────────────────

    def _on_connect(self, connector_id: str = "finam"):
        from core.connector_manager import connector_manager
        connector = connector_manager.get(connector_id)
        if not connector:
            return
        label = "Финам" if connector_id == "finam" else "QUIK"
        self._log(f"Подключение к {label}...", "info")
        threading.Thread(target=connector.connect, daemon=True).start()

    def _on_disconnect(self, connector_id: str = "finam"):
        from core.connector_manager import connector_manager
        connector = connector_manager.get(connector_id)
        if not connector:
            return
        label = "Финам" if connector_id == "finam" else "QUIK"
        self._log(f"Отключение от {label}...", "info")
        threading.Thread(target=connector.disconnect, daemon=True).start()

    def _update_connector_status(self, connector_id: str, connected: bool):
        lbl: QLabel = getattr(self, f"_lbl_{connector_id}", None)
        if not lbl:
            return
        label = "Финам" if connector_id == "finam" else "QUIK"
        if connected:
            lbl.setText(f"🟢  {label}: подключён")
            lbl.setStyleSheet("color: #a6e3a1; font-weight: bold; min-width: 170px;")
        else:
            lbl.setText(f"🔴  {label}: отключён")
            lbl.setStyleSheet("color: #f38ba8; font-weight: bold; min-width: 170px;")

        # Обновляем иконку в трее по любому из коннекторов
        from core.connector_manager import connector_manager
        any_connected = connector_manager.is_any_connected()
        self._tray.set_connected(any_connected)

    # ─────────────────────────────────────────────
    # Лог
    # ─────────────────────────────────────────────

    def _handle_log(self, message):
        try:
            record = message.record
            ui_signals.log_message.emit(
                record["message"], record["level"].name.lower()
            )
        except RuntimeError:
            # Qt объект уже удалён при завершении программы — игнорируем
            pass

    def _log(self, text: str, level: str = 'info'):
        ui_signals.log_message.emit(text, level)

    def _append_log(self, text: str, level: str):
        colors = {
            'debug': '#6c7086',
            'info': '#cdd6f4',
            'warning': '#f9e2af',
            'error': '#f38ba8',
            'critical': '#fab387',
        }
        color = colors.get(level, '#cdd6f4')
        html = self._format_log_html(text, color)

        if level != 'debug':
            self._append_log_to_view(self._log_views['Общий'], html)
        if self._is_connection_log(text):
            self._append_log_to_view(self._log_views['Соединение'], html)
        if self._is_position_log(text):
            self._append_log_to_view(self._log_views['Позиции'], html)
        if level == 'debug':
            self._append_log_to_view(self._log_views['Дебаг'], html)
        if self._is_error_log(text, level):
            self._append_log_to_view(self._log_views['Ошибки'], html)

    def _format_log_html(self, text: str, color: str) -> str:
        t = datetime.now().strftime('%H:%M:%S')
        safe_text = escape(text)
        return (
            f'<span style="color:#45475a">{t}</span>  '
            f'<span style="color:{color}">{safe_text}</span>'
        )

    def _append_log_to_view(self, view: QTextEdit, html: str):
        view.append(html)
        self._trim_log_view(view)
        sb = view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _trim_log_view(self, view: QTextEdit, max_blocks: int = 5000):
        doc = view.document()
        if doc.blockCount() <= max_blocks:
            return
        cursor = view.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        cursor.movePosition(
            cursor.MoveOperation.Down,
            cursor.MoveMode.KeepAnchor,
            doc.blockCount() - max_blocks,
        )
        cursor.removeSelectedText()

    def _clear_logs(self):
        for view in self._log_views.values():
            view.clear()

    def _is_connection_log(self, text: str) -> bool:
        text_lower = text.lower()
        keywords = (
            '[quik]', '[финам]', '[finam]', '[connectormanager]',
            'подключение', 'подключён', 'подключен', 'отключение', 'отключён', 'отключен',
            'коннектор', 'disconnect', 'reconnect', 'connect', 'lua-скрипт', 'серверу брокера',
        )
        return any(keyword in text_lower for keyword in keywords)

    def _is_position_log(self, text: str) -> bool:
        text_lower = text.lower()
        keywords = (
            'позиц', 'positionmanager', 'ордер', 'заявк', 'close_position', 'place_manual_order',
            'limit ', 'market ', ' chase ', 'filled=', 'tid=', ' buy ', ' sell ', ' close ', 'qty=',
        )
        return any(keyword in text_lower for keyword in keywords)

    def _is_error_log(self, text: str, level: str) -> bool:
        if level in {'error', 'critical'}:
            return True
        text_lower = text.lower()
        return 'ошиб' in text_lower or 'exception' in text_lower or 'traceback' in text_lower

    def _toggle_log(self):
        self._log_visible = not self._log_visible
        self.log_tabs.setVisible(self._log_visible)
        self.logbar.setFixedHeight(160 if self._log_visible else 26)
        self.btn_log_toggle.setText(
            'Свернуть' if self._log_visible else 'Развернуть'
        )

    # ─────────────────────────────────────────────
    # Настройки
    # ─────────────────────────────────────────────

    def _open_settings(self):
        """Переключается на вкладку настроек (Ctrl+S / кнопка в трее)."""
        self._tabs.setCurrentIndex(self._TAB_SETTINGS)

    def _restart_app(self):
        import subprocess
        self._save_column_state()
        subprocess.Popen([sys.executable] + sys.argv)
        QApplication.quit()

    # ─────────────────────────────────────────────
    # Сигналы и таймеры
    # ─────────────────────────────────────────────

    def _connect_signals(self):
        ui_signals.log_message.connect(self._append_log)
        ui_signals.connector_changed.connect(self._update_connector_status)
        ui_signals.strategies_changed.connect(self._refresh_table)
        ui_signals.positions_updated.connect(self._refresh_table)

    def _setup_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+N"), self).activated.connect(self._add_strategy)
        QShortcut(QKeySequence("Ctrl+Q"), self).activated.connect(self.close)
        QShortcut(QKeySequence("F5"), self).activated.connect(self._refresh_table)
        QShortcut(QKeySequence("Delete"), self).activated.connect(self._remove_strategy)
        QShortcut(QKeySequence("Ctrl+S"), self).activated.connect(self._open_settings)

    def _start_timers(self):
        self._t_clock = QTimer()
        self._t_clock.timeout.connect(self._update_clock)
        self._t_clock.start(1000)

        self._t_table = QTimer()
        self._t_table.timeout.connect(self._refresh_table)
        self._t_table.start(5000)

        self._t_events = QTimer()
        self._t_events.timeout.connect(self._update_next_event)
        self._t_events.start(30_000)
        self._update_next_event()

    def _update_clock(self):
        self.lbl_time.setText(
            f"🕐  {datetime.now().strftime('%d.%m.%Y  %H:%M:%S')}"
        )
        self._update_status_bar()

    def _update_status_bar(self):
        """Обновляет счётчики в статус-баре."""
        strategies = get_all_strategies()
        active = sum(1 for d in strategies.values() if d.get("status") == "active")
        total = len(strategies)
        self.lbl_strategies.setText(f"  📊 Агенты: {active}/{total}  ")

        from core.connector_manager import connector_manager
        parts = []
        for cid, conn in connector_manager.all().items():
            label = "Финам" if cid == "finam" else cid.upper()
            dot = "🟢" if conn.is_connected() else "🔴"
            parts.append(f"{dot} {label}")
        self.lbl_connectors.setText("  " + "  ".join(parts) + "  ")

    def _update_next_event(self):
        events = strategy_scheduler.get_next_events(1)
        if events:
            e = events[0]
            self.lbl_next_event.setText(
                f"⏰  {e['name']}  →  {e['next_run']}"
            )
        else:
            self.lbl_next_event.setText("Нет запланированных событий")

    # ─────────────────────────────────────────────
    # Сохранение/восстановление колонок
    # ─────────────────────────────────────────────

    def _save_column_state(self):
        settings = QSettings("TradingManager", "MainWindow")
        hh = self.table.horizontalHeader()
        settings.setValue("column_state", hh.saveState())
        # Порядок строк хранится в self._row_order и уже персистирован в _capture_row_order.
        # Здесь просто перезаписываем на случай закрытия без drag-операций.
        if self._row_order:
            settings.setValue("row_order", self._row_order)

    def _restore_row_order(self) -> list:
        """Возвращает сохранённый порядок sid или пустой список."""
        settings = QSettings("TradingManager", "MainWindow")
        return settings.value("row_order", []) or []

    def _on_row_order_changed(self, sids: list):
        """Вызывается из AgentTable после завершения drag-and-drop.

        sids — финальный порядок sid агентов. Сохраняем в памяти и QSettings,
        затем перестраиваем таблицу в новом порядке.
        """
        self._row_order = sids
        settings = QSettings("TradingManager", "MainWindow")
        settings.setValue("row_order", sids)
        self._refresh_table()

    def _restore_column_state(self):
        settings = QSettings("TradingManager", "MainWindow")
        # Сбрасываем кэш если кол-во колонок изменилось
        saved_count = settings.value("column_count", 0, type=int)
        if saved_count != len(COLUMNS):
            settings.remove("column_state")
            settings.setValue("column_count", len(COLUMNS))
            return
        state = settings.value("column_state")
        if state:
            self.table.horizontalHeader().restoreState(state)

    # ─────────────────────────────────────────────
    # Закрытие
    # ─────────────────────────────────────────────

    def closeEvent(self, event):
        self._save_column_state()
        minimize = get_bool_setting("minimize_to_tray")
        if minimize:
            event.ignore()
            self.hide()
            self._tray.notify(
                "Trading Strategy Manager",
                "Программа свёрнута в трей. Двойной клик — открыть.",
            )
            return

        reply = QMessageBox.question(
            self, "Выход",
            "Остановить все стратегии и выйти?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            event.ignore()
            return

        # ── Останавливаем всё ────────────────────────
        self._log("Завершение работы...", "info")

        # Таймеры
        for attr in ("_t_clock", "_t_table", "_t_events"):
            t = getattr(self, attr, None)
            if t:
                t.stop()

        # Планировщик
        try:
            strategy_scheduler.stop()
        except Exception as e:
            logger.warning(f"Ошибка остановки планировщика: {e}")

        # Коннекторы — сигнализируем reconnect-лупам остановиться
        from core.connector_manager import connector_manager
        for cid, connector in connector_manager.all().items():
            try:
                connector._stop_reconnect.set()  # стопим reconnect loop
                if connector.is_connected():
                    connector.disconnect()
                    logger.info(f"[{cid}] Отключён при завершении")
                # Деинициализация DLL если поддерживается
                if hasattr(connector, "shutdown"):
                    connector.shutdown()
            except Exception as e:
                logger.warning(f"[{cid}] Ошибка отключения: {e}")

        # Telegram
        try:
            notifier.send_raw("🛑 <b>Trading Manager остановлен</b>")
        except Exception:
            pass

        # Трей
        try:
            self._tray.hide()
        except Exception:
            pass

        event.accept()

        # Принудительный выход — убивает все daemon-потоки
        import sys
        sys.exit(0)


def run_app():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())