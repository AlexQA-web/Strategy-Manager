"""Mixin для логирования в MainWindow.

Выделен из MainWindow для уменьшения god-class.
Содержит методы обработки, фильтрации и отображения лог-сообщений.
"""

from datetime import datetime
from html import escape

from PyQt6.QtWidgets import QTextEdit


class LogMixin:
    """Mixin для MainWindow — обработка лог-сообщений."""

    _log_views: dict[str, QTextEdit]

    def _handle_log(self, message):
        try:
            record = message.record
            from ui.main_window import ui_signals
            ui_signals.log_message.emit(
                record["message"], record["level"].name.lower()
            )
        except RuntimeError:
            pass

    def _log(self, text: str, level: str = 'info'):
        from ui.main_window import ui_signals
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

    @staticmethod
    def _format_log_html(text: str, color: str) -> str:
        t = datetime.now().strftime('%H:%M:%S')
        safe_text = escape(text)
        return (
            f'<span style="color:#45475a">{t}</span>  '
            f'<span style="color:{color}">{safe_text}</span>'
        )

    @staticmethod
    def _append_log_to_view(view: QTextEdit, html: str):
        view.append(html)
        LogMixin._trim_log_view(view)
        sb = view.verticalScrollBar()
        sb.setValue(sb.maximum())

    @staticmethod
    def _trim_log_view(view: QTextEdit, max_blocks: int = 5000):
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

    @staticmethod
    def _is_connection_log(text: str) -> bool:
        text_lower = text.lower()
        keywords = (
            '[quik]', '[финам]', '[finam]', '[connectormanager]',
            'подключение', 'подключён', 'подключен', 'отключение', 'отключён', 'отключен',
            'коннектор', 'disconnect', 'reconnect', 'connect', 'lua-скрипт', 'серверу брокера',
        )
        return any(keyword in text_lower for keyword in keywords)

    @staticmethod
    def _is_position_log(text: str) -> bool:
        text_lower = text.lower()
        keywords = (
            'позиц', 'positionmanager', 'ордер', 'заявк', 'close_position', 'place_manual_order',
            'limit ', 'market ', ' chase ', 'filled=', 'tid=', ' buy ', ' sell ', ' close ', 'qty=',
        )
        return any(keyword in text_lower for keyword in keywords)

    @staticmethod
    def _is_error_log(text: str, level: str) -> bool:
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
