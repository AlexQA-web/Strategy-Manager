import sys
from PyQt6.QtWidgets import QSystemTrayIcon, QMenu, QApplication
from PyQt6.QtGui import QIcon, QPixmap, QColor, QPainter, QFont
from PyQt6.QtCore import Qt
from loguru import logger


def _make_tray_icon(connected: bool) -> QIcon:
    """
    Генерирует иконку трея программно (без внешних файлов).
    Зелёный круг = подключено, красный = отключено.
    """
    size = 32
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Фон — тёмный круг
    painter.setBrush(QColor("#1e1e2e"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(2, 2, size - 4, size - 4)

    # Цветной индикатор
    color = QColor("#a6e3a1") if connected else QColor("#f38ba8")
    painter.setBrush(color)
    painter.drawEllipse(8, 8, size - 16, size - 16)

    # Буква "T" (Trading)
    painter.setPen(QColor("#1e1e2e"))
    font = QFont("Arial", 10, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "T")

    painter.end()
    return QIcon(pixmap)


class TrayIcon(QSystemTrayIcon):
    """
    Иконка в системном трее.
    Поддерживает:
    - Показ/скрытие главного окна
    - Смену цвета при подключении/отключении
    - Контекстное меню
    - Balloon-уведомления
    """

    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self._window = main_window
        self._connected = False

        self.setIcon(_make_tray_icon(False))
        self.setToolTip("Trading Strategy Manager — отключён")

        self._build_menu()

        # Клик по иконке — показать/скрыть окно
        self.activated.connect(self._on_activated)

        self.show()
        logger.info("Системный трей инициализирован")

    # ─────────────────────────────────────────────
    # Меню
    # ─────────────────────────────────────────────

    def _build_menu(self):
        menu = QMenu()
        menu.setStyleSheet("""
            QMenu {
                background-color: #1e1e2e;
                color: #cdd6f4;
                border: 1px solid #313244;
                font-size: 13px;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 20px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #313244;
            }
            QMenu::separator {
                height: 1px;
                background-color: #313244;
                margin: 4px 0;
            }
        """)

        action_show = menu.addAction("📈  Показать окно")
        action_show.triggered.connect(self._show_window)

        menu.addSeparator()

        self.action_connect = menu.addAction("⚡  Подключить")
        self.action_connect.triggered.connect(self._connect)

        self.action_disconnect = menu.addAction("✖  Отключить")
        self.action_disconnect.triggered.connect(self._disconnect)

        menu.addSeparator()

        action_exit = menu.addAction("🚪  Выйти")
        action_exit.triggered.connect(self._exit)

        self.setContextMenu(menu)

    # ─────────────────────────────────────────────
    # Публичные методы
    # ─────────────────────────────────────────────

    def set_connected(self, connected: bool):
        """Обновляет иконку и подсказку при смене статуса коннектора."""
        self._connected = connected
        self.setIcon(_make_tray_icon(connected))
        if connected:
            self.setToolTip("Trading Strategy Manager — подключён ✓")
        else:
            self.setToolTip("Trading Strategy Manager — отключён")

    def notify(self, title: str, message: str, duration_ms: int = 4000):
        """Показывает balloon-уведомление от трея."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = (
            QSystemTrayIcon.MessageIcon.Information
            if "ошибк" not in message.lower()
            else QSystemTrayIcon.MessageIcon.Warning
        )
        self.showMessage(title, message, icon, duration_ms)

    # ─────────────────────────────────────────────
    # Обработчики
    # ─────────────────────────────────────────────

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            # Одиночный клик — показать/скрыть
            if self._window.isVisible():
                self._window.hide()
            else:
                self._show_window()
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_window()

    def _show_window(self):
        self._window.showNormal()
        self._window.raise_()
        self._window.activateWindow()

    def _connect(self):
        import threading
        from core.connector_manager import connector_manager
        for cid, connector in connector_manager.all().items():
            if not connector.is_connected():
                threading.Thread(target=connector.connect, daemon=True).start()

    def _disconnect(self):
        import threading
        from core.connector_manager import connector_manager
        for cid, connector in connector_manager.all().items():
            if connector.is_connected():
                threading.Thread(target=connector.disconnect, daemon=True).start()

    def _exit(self):
        from PyQt6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            None,
            "Выход",
            "Остановить все стратегии и выйти?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            from core.scheduler import strategy_scheduler
            from core.connector_manager import connector_manager
            from core.telegram_bot import notifier
            strategy_scheduler.stop()
            for cid, connector in connector_manager.all().items():
                if connector.is_connected():
                    connector.disconnect()
            notifier.send_raw("🛑 <b>Trading Manager остановлен</b>")
            self.hide()
            QApplication.quit()
