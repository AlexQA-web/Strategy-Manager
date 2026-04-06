from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QHeaderView, QDialog, QFormLayout,
    QSpinBox, QDialogButtonBox, QMessageBox, QAbstractItemView,
)
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QMetaObject, Q_ARG, QTimer
from PyQt6.QtGui import QColor
from loguru import logger
from core.position_manager import position_manager
from ui.ui_safety import (
    DestructiveActionGuard,
    build_account_close_confirmation,
    build_strategy_close_confirmation,
)


# ──────────────────────────────────────────────────────────────────────────────
# Вспомогательный класс для thread-safe вызова UI из другого потока
# ──────────────────────────────────────────────────────────────────────────────
class _Bridge(QObject):
    refresh_signal = pyqtSignal()


# ──────────────────────────────────────────────────────────────────────────────
# Диалог частичного закрытия
# ──────────────────────────────────────────────────────────────────────────────
class PartialCloseDialog(QDialog):
    def __init__(self, ticker: str, max_qty: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Частичное закрытие — {ticker}")
        self.setFixedWidth(300)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)

        self._spin = QSpinBox()
        self._spin.setRange(1, max_qty)
        self._spin.setValue(max_qty)
        self._spin.setSuffix(" лот.")
        self._spin.setFixedWidth(120)
        form.addRow("Объём закрытия:", self._spin)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def quantity(self) -> int:
        return self._spin.value()


# ──────────────────────────────────────────────────────────────────────────────
# Основная панель позиций
# ──────────────────────────────────────────────────────────────────────────────
class PositionsPanel(QWidget):
    """
    Таблица открытых позиций с кнопками «Закрыть» и «Закрыть всё».
    account_id — счёт Финам, по которому отображаются позиции.
    ticker     — если задан, показывает только позиции по этому тикеру.
    live_engine — если задан, берёт позицию и цену из LiveEngine (агент-специфично).
    strategy_id — если задан, использует strategy-scoped flatten вместо account-level close.
    """

    def __init__(self, account_id: str | None = None, ticker: str | None = None,
                 live_engine=None, strategy_id: str | None = None, parent=None):
        super().__init__(parent)
        self._account_id = account_id
        self._ticker = ticker
        self._live_engine = live_engine
        self._strategy_id = strategy_id
        self._action_guard = DestructiveActionGuard()
        self._bridge = _Bridge()
        self._bridge.refresh_signal.connect(self._refresh_table)

        self._build_ui()

        # Подписка на обновления от position_manager
        position_manager.on_update(self._on_positions_updated)

        # Первоначальная загрузка
        self._refresh_table()

        # Таймер для обновления цены из LiveEngine (каждые 5 сек)
        if self._live_engine is not None:
            self._price_timer = QTimer(self)
            self._price_timer.setInterval(5000)
            self._price_timer.timeout.connect(self._refresh_table)
            self._price_timer.start()

    # ── Построение UI ────────────────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Заголовок + кнопки
        top = QHBoxLayout()
        title = "Позиции стратегии" if self._strategy_id else "Открытые позиции"
        self._lbl_title = QLabel(title)
        self._lbl_title.setStyleSheet("font-weight: bold; font-size: 13px;")
        top.addWidget(self._lbl_title)
        top.addStretch()

        self._btn_refresh = QPushButton("🔄 Обновить")
        self._btn_refresh.setFixedWidth(110)
        self._btn_refresh.clicked.connect(self._on_refresh_clicked)
        top.addWidget(self._btn_refresh)

        if self._strategy_id:
            self._btn_close_strategy = QPushButton("✖ Закрыть стратегию")
            self._btn_close_strategy.setFixedWidth(150)
            self._btn_close_strategy.setStyleSheet("color: #f38ba8;")
            self._btn_close_strategy.clicked.connect(self._on_close_all_strategy)
            top.addWidget(self._btn_close_strategy)

            self._btn_close_account_all = QPushButton("❌ Закрыть счёт")
            self._btn_close_account_all.setFixedWidth(130)
            self._btn_close_account_all.clicked.connect(self._on_close_all_account)
            top.addWidget(self._btn_close_account_all)
        else:
            self._btn_close_all = QPushButton("❌ Закрыть всё")
            self._btn_close_all.setFixedWidth(120)
            self._btn_close_all.setStyleSheet("color: #f38ba8;")
            self._btn_close_all.clicked.connect(self._on_close_all_account)
            top.addWidget(self._btn_close_all)

        layout.addLayout(top)

        # Таблица
        self._table = QTableWidget()
        self._table.setColumnCount(7)
        self._table.setHorizontalHeaderLabels([
            "Тикер", "Направление", "Объём", "Ср. цена", "Тек. цена", "P&L", "Действия"
        ])
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(6, 200)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        layout.addWidget(self._table)

        # Итоговый P&L
        self._lbl_total_pnl = QLabel("Суммарный P&L: —")
        self._lbl_total_pnl.setStyleSheet("font-size: 12px; padding: 2px 0;")
        layout.addWidget(self._lbl_total_pnl)

    # ── Обновление таблицы ───────────────────────────────────────────────────

    def _on_positions_updated(self):
        """Вызывается из фонового потока → пробрасываем в UI поток через сигнал."""
        self._bridge.refresh_signal.emit()

    def _refresh_table(self):
        # Если есть LiveEngine — показываем только его позицию
        if self._live_engine is not None:
            pos_info = self._live_engine.get_position_info()
            if pos_info["quantity"] == 0:
                positions = []
            else:
                positions = [pos_info]
        elif self._strategy_id:
            positions = self._get_strategy_positions()
        elif self._account_id:
            positions = position_manager.get_positions(self._account_id)
            if self._ticker:
                positions = [p for p in positions if p.get("ticker") == self._ticker]
        else:
            positions = position_manager.get_all_positions()
            if self._ticker:
                positions = [p for p in positions if p.get("ticker") == self._ticker]

        self._table.setRowCount(0)
        total_pnl = 0.0

        for pos in positions:
            row = self._table.rowCount()
            self._table.insertRow(row)

            ticker    = pos.get("ticker", "—")
            side      = pos.get("side", "—")
            quantity  = float(pos.get("quantity", 0))
            avg_price = float(pos.get("avg_price", 0))
            cur_price = float(pos.get("current_price", 0))
            pnl_raw   = pos.get("pnl")
            pnl       = float(pnl_raw) if pnl_raw is not None else None
            if pnl is not None:
                total_pnl += pnl

            # Цвет направления
            side_item = QTableWidgetItem("🟢 BUY" if side == "buy" else "🔴 SELL")
            side_item.setForeground(
                QColor("#a6e3a1") if side == "buy" else QColor("#f38ba8")
            )

            # P&L с цветом
            pnl_item = QTableWidgetItem("—" if pnl is None else f"{pnl:+.2f} ₽")
            pnl_item.setForeground(
                QColor("#6c7086") if pnl is None else QColor("#a6e3a1") if pnl >= 0 else QColor("#f38ba8")
            )
            pnl_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            self._table.setItem(row, 0, QTableWidgetItem(ticker))
            self._table.setItem(row, 1, side_item)
            self._table.setItem(row, 2, QTableWidgetItem(str(int(quantity))))
            self._table.setItem(row, 3, QTableWidgetItem(f"{avg_price:.2f}"))
            self._table.setItem(row, 4, QTableWidgetItem(f"{cur_price:.2f}"))
            self._table.setItem(row, 5, pnl_item)

            # Кнопки действий
            btn_widget = QWidget()
            btn_layout = QHBoxLayout(btn_widget)
            btn_layout.setContentsMargins(4, 2, 4, 2)
            btn_layout.setSpacing(4)

            btn_partial = QPushButton("📉 Частично")
            btn_partial.setFixedHeight(24)
            btn_partial.clicked.connect(lambda _, t=ticker: self._on_partial_close(t))

            btn_close = QPushButton("✖ Закрыть")
            btn_close.setFixedHeight(24)
            btn_close.setStyleSheet("color: #f38ba8;")
            btn_close.clicked.connect(
                lambda _, t=ticker: self._on_close_position(t)
            )

            btn_layout.addWidget(btn_partial)
            btn_layout.addWidget(btn_close)
            self._table.setCellWidget(row, 6, btn_widget)

        # Обновляем суммарный P&L
        known_pnl = [pos.get("pnl") for pos in positions if pos.get("pnl") is not None]
        if known_pnl and len(known_pnl) == len(positions):
            color = "#a6e3a1" if total_pnl >= 0 else "#f38ba8"
            self._lbl_total_pnl.setText(f"Суммарный P&L: <b>{total_pnl:+.2f} ₽</b>")
            self._lbl_total_pnl.setStyleSheet(f"font-size: 12px; color: {color};")
        else:
            self._lbl_total_pnl.setText("Суммарный P&L: <b>—</b>")
            self._lbl_total_pnl.setStyleSheet("font-size: 12px; color: #6c7086;")

        # Заголовок с количеством позиций
        base_title = "Позиции стратегии" if self._strategy_id else "Открытые позиции"
        self._lbl_title.setText(f"{base_title} ({self._table.rowCount()})")

    def _get_strategy_positions(self) -> list[dict]:
        from core.strategy_position_book import get_strategy_position_book

        entries = get_strategy_position_book(self._strategy_id, ticker=self._ticker)
        broker_positions = []
        if self._account_id:
            broker_positions = position_manager.get_positions(self._account_id)

        positions = []
        for entry in entries:
            broker_match = next(
                (
                    pos for pos in broker_positions
                    if str(pos.get("ticker", "")).upper() == entry["ticker"]
                    and str(pos.get("board", entry["board"])).upper() == entry["board"]
                ),
                {},
            )
            positions.append({
                "ticker": entry["ticker"],
                "side": entry["side"],
                "quantity": entry["quantity"],
                "avg_price": entry["avg_entry_price"],
                "current_price": float(
                    broker_match.get("current_price", broker_match.get("avg_price", 0.0)) or 0.0
                ),
                "pnl": None,
            })
        return positions

    def _action_widgets(self, trigger_widget=None) -> list:
        widgets = [
            self._btn_refresh,
            getattr(self, "_btn_close_strategy", None),
            getattr(self, "_btn_close_account_all", None),
            getattr(self, "_btn_close_all", None),
            trigger_widget,
        ]
        return [widget for widget in widgets if widget is not None]

    def _run_destructive_action(self, action, trigger_widget=None) -> bool:
        return self._action_guard.run(self._action_widgets(trigger_widget), action)

    # ── Обработчики кнопок ───────────────────────────────────────────────────

    def _on_refresh_clicked(self):
        if self._account_id:
            position_manager.refresh(self._account_id)
        else:
            self._refresh_table()

    def _on_close_position(self, ticker: str):
        trigger_widget = self.sender()
        if self._strategy_id:
            reply = QMessageBox.question(
                self,
                "Подтверждение",
                build_strategy_close_confirmation(self._strategy_id, ticker=ticker),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                result = {}

                def _action():
                    nonlocal result
                    result = position_manager.close_strategy_position(self._strategy_id, ticker=ticker)

                self._run_destructive_action(_action, trigger_widget=trigger_widget)
                if result.get("status") not in {"submitted", "success", "no_position"}:
                    QMessageBox.warning(
                        self,
                        "Ошибка",
                        f"Не удалось закрыть позицию стратегии {self._strategy_id} по {ticker}: "
                        f"{result.get('status')}",
                    )
            return

        reply = QMessageBox.question(
            self,
            "Подтверждение",
            f"Закрыть позицию {ticker} полностью по рыночной цене?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            account = self._account_id or self._detect_account(ticker)
            result = {"ok": False}

            def _action():
                result["ok"] = position_manager.close_position(account, ticker)

            self._run_destructive_action(_action, trigger_widget=trigger_widget)
            ok = result["ok"]
            if not ok:
                QMessageBox.warning(self, "Ошибка", f"Не удалось закрыть позицию {ticker}")

    def _resolve_current_position_qty(self, ticker: str) -> int:
        ticker_upper = str(ticker or "").upper()
        if self._live_engine is not None:
            pos_info = self._live_engine.get_position_info()
            if str(pos_info.get("ticker", "")).upper() == ticker_upper:
                return abs(int(float(pos_info.get("quantity", 0) or 0)))
            return 0
        if self._strategy_id:
            for pos in self._get_strategy_positions():
                if str(pos.get("ticker", "")).upper() == ticker_upper:
                    return abs(int(float(pos.get("quantity", 0) or 0)))
            return 0
        positions = position_manager.get_positions(self._account_id) if self._account_id else position_manager.get_all_positions()
        for pos in positions:
            if str(pos.get("ticker", "")).upper() == ticker_upper:
                return abs(int(float(pos.get("quantity", 0) or 0)))
        return 0

    def _on_partial_close(self, ticker: str):
        trigger_widget = self.sender()
        max_qty = self._resolve_current_position_qty(ticker)
        if max_qty <= 0:
            QMessageBox.warning(
                self,
                "Позиция обновилась",
                f"Актуальная позиция по {ticker} уже отсутствует или равна нулю.",
            )
            self._refresh_table()
            return
        dlg = PartialCloseDialog(ticker, max_qty, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            qty = dlg.quantity()
            if self._strategy_id:
                result = {}

                def _action():
                    nonlocal result
                    result = position_manager.close_strategy_position(
                        self._strategy_id,
                        ticker=ticker,
                        quantity=qty,
                    )

                self._run_destructive_action(_action, trigger_widget=trigger_widget)
                if result.get("status") not in {"submitted", "success", "no_position"}:
                    QMessageBox.warning(
                        self,
                        "Ошибка",
                        f"Не удалось частично закрыть стратегию {self._strategy_id} по {ticker}: "
                        f"{result.get('status')}",
                    )
                return

            account = self._account_id or self._detect_account(ticker)
            result = {"ok": False}

            def _action():
                result["ok"] = position_manager.close_position(account, ticker, quantity=qty)

            self._run_destructive_action(_action, trigger_widget=trigger_widget)
            ok = result["ok"]
            if not ok:
                QMessageBox.warning(self, "Ошибка", f"Не удалось частично закрыть {ticker}")

    def _on_close_all_strategy(self):
        if self._table.rowCount() == 0:
            return
        reply = QMessageBox.question(
            self,
            "Подтверждение",
            build_strategy_close_confirmation(self._strategy_id),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            result = {}

            def _action():
                nonlocal result
                result = position_manager.close_strategy_positions(self._strategy_id)

            self._run_destructive_action(_action, trigger_widget=self.sender())
            if result.get("status") not in {"submitted", "success", "no_position"}:
                QMessageBox.warning(
                    self,
                    "Ошибка",
                    f"Не удалось закрыть позиции стратегии {self._strategy_id}: "
                    f"{result.get('status')}",
                )

    def _on_close_all_account(self):
        if self._table.rowCount() == 0:
            return
        reply = QMessageBox.question(
            self,
            "Подтверждение",
            build_account_close_confirmation(),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            account = self._account_id or ""
            result = {"closed": 0}

            def _action():
                result["closed"] = position_manager.close_all_positions(account)

            self._run_destructive_action(_action, trigger_widget=self.sender())
            closed = result["closed"]
            QMessageBox.information(self, "Готово", f"Закрыто позиций: {closed}")

    def _detect_account(self, ticker: str) -> str:
        """Находит account_id для тикера из текущих позиций."""
        all_pos = position_manager.get_all_positions()
        for pos in all_pos:
            if pos.get("ticker") == ticker:
                return pos.get("account_id", "")
        return ""

    # ── Публичный метод ──────────────────────────────────────────────────────

    def set_account(self, account_id: str):
        """Переключает панель на другой счёт и обновляет таблицу."""
        self._account_id = account_id
        self._refresh_table()

    def closeEvent(self, event):
        position_manager.remove_update_callback(self._on_positions_updated)
        super().closeEvent(event)
