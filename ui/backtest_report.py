# ui/backtest_report.py

import bisect
import csv
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from loguru import logger
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QTabWidget, QWidget,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QFileDialog, QFrame, QSizePolicy,
)

from core.backtest_engine import BacktestResult, Trade

# ── Цветовая схема (Catppuccin Mocha) ────────────────────────────────────────

COLORS = {
    "bg":      "#1e1e2e",
    "surface": "#181825",
    "overlay": "#313244",
    "border":  "#45475a",
    "text":    "#cdd6f4",
    "subtext": "#a6adc8",
    "muted":   "#6c7086",
    "blue":    "#89b4fa",
    "green":   "#a6e3a1",
    "red":     "#f38ba8",
    "yellow":  "#f9e2af",
    "teal":    "#94e2d5",
}

STYLE = f"""
QDialog {{
    background: {COLORS['bg']};
    color: {COLORS['text']};
    font-family: 'Segoe UI', sans-serif;
    font-size: 13px;
}}
QTabWidget::pane {{
    border: 1px solid {COLORS['border']};
    border-radius: 6px;
    background: {COLORS['surface']};
}}
QTabBar::tab {{
    background: {COLORS['overlay']};
    color: {COLORS['subtext']};
    padding: 7px 20px;
    border-radius: 4px 4px 0 0;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: {COLORS['surface']};
    color: {COLORS['blue']};
    font-weight: bold;
}}
QTableWidget {{
    background: {COLORS['surface']};
    color: {COLORS['text']};
    border: none;
    gridline-color: {COLORS['border']};
    font-size: 12px;
}}
QTableWidget::item:selected {{ background: {COLORS['overlay']}; }}
QHeaderView::section {{
    background: {COLORS['overlay']};
    color: {COLORS['blue']};
    padding: 5px 8px;
    border: none;
    border-right: 1px solid {COLORS['border']};
    font-weight: bold;
    font-size: 12px;
}}
QPushButton {{
    border-radius: 5px;
    padding: 7px 18px;
    font-weight: bold;
}}
QPushButton#btn_export {{
    background: {COLORS['overlay']};
    color: {COLORS['text']};
    border: 1px solid {COLORS['border']};
}}
QPushButton#btn_export:hover {{ background: {COLORS['border']}; }}
QPushButton#btn_close {{
    background: {COLORS['blue']};
    color: {COLORS['bg']};
    border: none;
}}
QPushButton#btn_close:hover {{ background: #b4befe; }}
QFrame#divider {{
    background: {COLORS['border']};
    max-height: 1px;
}}
QLabel#header {{
    font-size: 16px;
    font-weight: bold;
    color: {COLORS['blue']};
}}
QLabel#subheader {{
    font-size: 11px;
    color: {COLORS['muted']};
}}
"""


class MetricCard(QFrame):
    def __init__(self, title: str, value: str, color: str = COLORS["text"], parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['overlay']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
                padding: 4px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setSpacing(2)
        lay.setContentsMargins(12, 8, 12, 8)

        lbl_title = QLabel(title)
        lbl_title.setStyleSheet(f"color: {COLORS['muted']}; font-size: 11px;")
        lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lbl_value = QLabel(value)
        lbl_value.setStyleSheet(f"color: {color}; font-size: 18px; font-weight: bold;")
        lbl_value.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lay.addWidget(lbl_title)
        lay.addWidget(lbl_value)


class BacktestReport(QDialog):
    def __init__(self, result: BacktestResult, parent=None):
        super().__init__(parent)
        self._result = result
        self.setWindowTitle("Результаты бэктеста")
        self.setMinimumSize(900, 680)
        self.setStyleSheet(STYLE)
        self._build_ui()
        logger.debug("BacktestReport открыт")

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(16, 16, 16, 16)

        root.addLayout(self._build_header())
        root.addWidget(self._build_cards())
        root.addWidget(self._build_divider())

        tabs = QTabWidget()
        tabs.addTab(self._build_chart_tab(), "📈 Equity curve")
        tabs.addTab(self._build_trades_tab(), "📋 Сделки")
        root.addWidget(tabs, stretch=1)
        root.addLayout(self._build_buttons())

    def _build_header(self) -> QHBoxLayout:
        lay = QHBoxLayout()
        r = self._result

        title = QLabel(f"Бэктест: {r.ticker}")
        title.setObjectName("header")

        sub = QLabel(
            f"{r.date_from.date()} → {r.date_to.date()} "
            f"| {r.bars_count:,} баров"
        )
        sub.setObjectName("subheader")

        lay.addWidget(title)
        lay.addStretch()
        lay.addWidget(sub)
        return lay

    def _build_cards(self) -> QWidget:
        r = self._result
        widget = QWidget()
        grid = QGridLayout(widget)
        grid.setSpacing(8)

        pnl_color = COLORS["green"] if r.total_net_pnl >= 0 else COLORS["red"]
        dd_color  = COLORS["red"]   if r.max_drawdown > 0    else COLORS["green"]
        pf_color  = COLORS["green"] if r.profit_factor >= 1.0 else COLORS["red"]

        cards = [
            ("Net P&L",        f"{r.total_net_pnl:+.2f} ₽",    pnl_color),
            ("Gross P&L",      f"{r.total_gross_pnl:+.2f} ₽",  pnl_color),
            ("Комиссия",       f"{r.total_commission:.2f} ₽",   COLORS["yellow"]),
            ("Сделок",         str(r.trades_count),             COLORS["text"]),
            ("Win Rate",       f"{r.win_rate:.1f}%",            COLORS["blue"]),
            ("Profit Factor",  f"{r.profit_factor:.2f}",        pf_color),
            ("Recovery Factor",f"{r.recovery_factor:.2f}",      COLORS["teal"]),
            ("Avg Win",        f"{r.avg_win:+.2f} ₽",          COLORS["green"]),
            ("Avg Loss",       f"{r.avg_loss:+.2f} ₽",         COLORS["red"]),
            ("Max Drawdown",   f"{r.max_drawdown:.2f} ₽",      dd_color),
            ("Sharpe Ratio",   f"{r.sharpe_ratio:.2f}",         COLORS["teal"]),
        ]

        for idx, (title, value, color) in enumerate(cards):
            grid.addWidget(MetricCard(title, value, color), idx // 5, idx % 5)

        return widget

    def _build_divider(self) -> QFrame:
        f = QFrame()
        f.setObjectName("divider")
        f.setFrameShape(QFrame.Shape.HLine)
        return f

    def _build_chart_tab(self) -> QWidget:
        widget = QWidget()
        lay = QVBoxLayout(widget)
        lay.setContentsMargins(0, 8, 0, 0)

        canvas = FigureCanvas(self._build_equity_figure())
        canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lay.addWidget(canvas)
        return widget

    def _build_equity_figure(self) -> Figure:
        r = self._result
        trades = [t for t in r.trades if t.is_closed]
        dates  = [dt  for dt, _   in r.equity_curve]
        equity = [pnl for _,  pnl in r.equity_curve]

        # Быстрый поиск equity по дате — bisect импортирован на уровне модуля
        def equity_at(dt):
            idx = bisect.bisect_right(dates, dt) - 1
            return equity[idx] if idx >= 0 else 0.0

        # Децимация: не более 1000 точек на графике
        step = max(1, len(dates) // 1000)
        dates_d  = dates[::step]
        equity_d = equity[::step]

        fig = Figure(figsize=(10, 5), facecolor=COLORS["bg"])
        ax  = fig.add_subplot(111, facecolor=COLORS["surface"])

        ax.plot(dates_d, equity_d, color=COLORS["blue"], linewidth=1.5, zorder=3)
        ax.fill_between(dates_d, equity_d, 0,
                        where=[e >= 0 for e in equity_d],
                        color=COLORS["green"], alpha=0.15, zorder=2)
        ax.fill_between(dates_d, equity_d, 0,
                        where=[e < 0 for e in equity_d],
                        color=COLORS["red"], alpha=0.15, zorder=2)

        for trade in trades:
            entry_color = COLORS["green"] if trade.direction == 1 else COLORS["red"]
            ax.scatter(trade.entry_dt, equity_at(trade.entry_dt),
                       color=entry_color, s=30, zorder=5, marker="^")
            ax.scatter(trade.exit_dt, equity_at(trade.exit_dt),
                       color=COLORS["yellow"], s=25, zorder=5, marker="v")

        ax.axhline(0, color=COLORS["border"], linewidth=0.8, linestyle="--")
        ax.set_title("Equity Curve", color=COLORS["text"], pad=10, fontsize=13)
        ax.tick_params(colors=COLORS["subtext"], labelsize=10)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
        fig.autofmt_xdate(rotation=30)
        for spine in ax.spines.values():
            spine.set_edgecolor(COLORS["border"])
        ax.set_ylabel("P&L, ₽", color=COLORS["subtext"])
        ax.grid(True, color=COLORS["border"], alpha=0.4, linewidth=0.5)
        fig.tight_layout()
        return fig

    def _build_trades_tab(self) -> QWidget:
        widget = QWidget()
        lay = QVBoxLayout(widget)
        lay.setContentsMargins(0, 8, 0, 0)

        trades = [t for t in self._result.trades if t.is_closed]
        columns = [
            "#", "Направление", "Вход (дата)",
            "Цена входа", "Выход (дата)", "Цена выхода",
            "Gross P&L", "Комиссия", "Net P&L", "Комментарий",
        ]

        table = QTableWidget(len(trades), len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.setStyleSheet(
            table.styleSheet() +
            f"QTableWidget {{ alternate-background-color: {COLORS['overlay']}; }}"
        )
        table.verticalHeader().setVisible(False)

        for row_idx, trade in enumerate(trades):
            direction = "Long" if trade.direction == 1 else "Short"
            dir_color = COLORS["green"] if trade.direction == 1 else COLORS["red"]
            pnl_color = COLORS["green"] if trade.net_pnl >= 0 else COLORS["red"]

            cells = [
                (str(row_idx + 1),                            COLORS["muted"]),
                (direction,                                   dir_color),
                (trade.entry_dt.strftime("%Y-%m-%d %H:%M"),  COLORS["text"]),
                (f"{trade.entry_price:.4f}",                  COLORS["text"]),
                (trade.exit_dt.strftime("%Y-%m-%d %H:%M"),   COLORS["text"]),
                (f"{trade.exit_price:.4f}",                   COLORS["text"]),
                (f"{trade.gross_pnl:+.2f}",                  pnl_color),
                (f"{trade.commission:.2f}",                   COLORS["yellow"]),
                (f"{trade.net_pnl:+.2f}",                    pnl_color),
                (trade.exit_comment,                          COLORS["muted"]),
            ]

            for col_idx, (text, color) in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setForeground(QColor(color))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                table.setItem(row_idx, col_idx, item)

        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(9, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(table)
        return widget

    def _build_buttons(self) -> QHBoxLayout:
        lay = QHBoxLayout()
        lay.setSpacing(8)

        btn_export = QPushButton("💾 Экспорт CSV")
        btn_export.setObjectName("btn_export")
        btn_export.clicked.connect(self._export_csv)

        btn_close = QPushButton("Закрыть")
        btn_close.setObjectName("btn_close")
        btn_close.clicked.connect(self.accept)

        lay.addStretch()
        lay.addWidget(btn_export)
        lay.addWidget(btn_close)
        return lay

    def _export_csv(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить отчёт", "backtest_result.csv", "CSV files (*.csv)"
        )
        if not path:
            return

        trades = [t for t in self._result.trades if t.is_closed]
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "#", "Direction", "Entry date", "Entry price",
                    "Exit date", "Exit price",
                    "Gross P&L", "Commission", "Net P&L", "Comment",
                ])
                for i, t in enumerate(trades, 1):
                    writer.writerow([
                        i,
                        "Long" if t.direction == 1 else "Short",
                        t.entry_dt.strftime("%Y-%m-%d %H:%M"),
                        t.entry_price,
                        t.exit_dt.strftime("%Y-%m-%d %H:%M"),
                        t.exit_price,
                        round(t.gross_pnl, 4),
                        round(t.commission, 4),
                        round(t.net_pnl, 4),
                        t.exit_comment,
                    ])
            logger.info(f"Экспорт CSV: {path}")
        except Exception as e:
            logger.error(f"Ошибка экспорта: {e}")
