# ui/chart_window.py
#
# Роль: Окно графика цены для стратегии торгового менеджера.
# Отображает свечной график (OHLCV) с индикаторами стратегии, маркерами сделок,
# crosshair, текущей ценой и автообновлением по закрытию бара.
#
# Реализовано на pyqtgraph (вместо matplotlib) для высокой производительности
# при pan/zoom и обновлении данных в реальном времени.
#
# Вызывается из: ui/strategy_window.py (кнопка График)
# Потребляет: core/connector_manager, core/storage, core/order_history, core/chart_cache
# Особенности: числовой индекс по оси X (без пробелов ночей/выходных),
#              нативный pan/zoom pyqtgraph ViewBox, QThread для загрузки данных.

from __future__ import annotations

import importlib
import math
import re
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPen, QBrush, QPainter
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QSizePolicy, QProgressBar, QComboBox,
)
from loguru import logger

from core.connector_manager import connector_manager
from core.order_history import get_order_pairs
from core.storage import get_strategy

# ─────────────────────────────────────────────
# Конфиг цветов (Catppuccin Mocha)
# ─────────────────────────────────────────────

CANDLE_UP   = "#a6e3a1"
CANDLE_DOWN = "#f38ba8"
BG_MAIN     = "#181825"
BG_FIGURE   = "#1e1e2e"
GRID_COLOR  = "#2a2a3e"
TICK_COLOR  = "#6c7086"
CROSS_COLOR = "#89b4fa"

PERIODS = {
    "1 день":   {"days": 1,   "tf_default": "1m"},
    "1 неделя": {"days": 7,   "tf_default": "15m"},
    "1 месяц":  {"days": 30,  "tf_default": "1h"},
    "3 месяца": {"days": 90,  "tf_default": "4h"},
    "1 год":    {"days": 365, "tf_default": "1d"},
}

TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

STYLE_DIALOG = """
QDialog, QWidget {
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: Segoe UI, Arial;
    font-size: 13px;
}
QPushButton {
    background-color: #313244;
    color: #cdd6f4;
    border: none;
    border-radius: 4px;
    padding: 4px 12px;
}
QPushButton:hover   { background-color: #45475a; }
QPushButton:checked {
    background-color: #89b4fa;
    color: #1e1e2e;
    font-weight: bold;
}
QPushButton#btn_refresh {
    background-color: #a6e3a1;
    color: #1e1e2e;
    font-weight: bold;
}
QComboBox {
    background-color: #313244;
    border: 1px solid #45475a;
    border-radius: 4px;
    padding: 3px 8px;
    color: #cdd6f4;
    min-width: 70px;
}
QComboBox::drop-down { border: none; }
QComboBox QAbstractItemView {
    background-color: #313244;
    color: #cdd6f4;
    selection-background-color: #45475a;
    border: 1px solid #45475a;
}
QLabel#lbl_title {
    font-size: 15px;
    font-weight: bold;
    color: #89b4fa;
}
QLabel#lbl_status {
    color: #6c7086;
    font-size: 11px;
    padding: 2px 12px;
}
QProgressBar {
    background-color: #181825;
    border: none;
    border-radius: 2px;
    height: 3px;
}
QProgressBar::chunk {
    background-color: #89b4fa;
    border-radius: 2px;
}
"""


# ─────────────────────────────────────────────
# Утилиты
# ─────────────────────────────────────────────

def _parse_trade_time(order: dict) -> datetime:
    comment = order.get("comment", "")
    m = re.search(r"time=(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}:\d{2})", comment)
    if m:
        try:
            return datetime.strptime(m.group(1), "%d.%m.%Y %H:%M:%S")
        except ValueError:
            pass
    return datetime.fromisoformat(order["timestamp"])


def _price_decimals(prices) -> int:
    counts = []
    for p in prices:
        try:
            if p != p or p == 0:
                continue
            frac = abs(p - math.floor(p))
            if frac < 1e-9:
                counts.append(0)
                continue
            s = f"{frac:.8f}".rstrip("0")
            counts.append(min(len(s) - 2, 6))
        except Exception:
            continue
    if not counts:
        return 2
    return max(set(counts), key=counts.count)


def _mk_pen(hex_str: str, width: float = 1.0, style=Qt.PenStyle.SolidLine) -> QPen:
    p = QPen(QColor(hex_str), width, style)
    p.setCosmetic(True)
    return p


# ─────────────────────────────────────────────
# DataLoader
# ─────────────────────────────────────────────

class DataLoader(QThread):
    data_ready = pyqtSignal(object)
    error      = pyqtSignal(str)

    def __init__(self, ticker: str, board: str, days: int,
                 interval: str, connector_id: str = "finam",
                 precalc_fn=None):
        super().__init__()
        self.ticker       = ticker
        self.board        = board
        self.days         = days
        self.interval     = interval
        self.connector_id = connector_id
        self._cancelled   = False
        # precalc_fn(df) — тяжёлые вычисления стратегии (on_precalc).
        # Вызывается внутри QThread чтобы не блокировать GUI поток.
        self._precalc_fn  = precalc_fn

    def _apply_precalc(self, df: pd.DataFrame) -> pd.DataFrame:
        if self._precalc_fn is None or df is None or df.empty:
            return df
        try:
            return self._precalc_fn(df)
        except Exception as e:
            logger.warning(f"[DataLoader] precalc error: {e}", exc_info=True)
            return df

    def run(self):
        import time as _time
        import threading as _threading
        logger.debug(f"[DataLoader] run start thread={_threading.current_thread().name}")
        try:
            from core import chart_cache
            cached = chart_cache.load(self.ticker, self.interval)
            if cached is not None and not cached.empty:
                # Проверяем покрывает ли кеш нужный диапазон дней.
                # Если кеш короче чем self.days — перегружаем полностью.
                first_dt = cached.index[0].to_pydatetime()
                cache_days = (datetime.now() - first_dt).days
                needs_full_reload = cache_days < self.days - 2  # допуск 2 дня

                if needs_full_reload:
                    logger.debug(f"[DataLoader] кеш покрывает {cache_days}д < {self.days}д — полная перезагрузка")
                    df = self._load_history()
                    if df is not None and not df.empty:
                        chart_cache.save(self.ticker, self.interval, df)
                        t0 = _time.monotonic()
                        result = self._apply_precalc(df)
                        logger.debug(f"[DataLoader] precalc(full_reload) done in {_time.monotonic()-t0:.2f}s")
                        self.data_ready.emit(result)
                    else:
                        # Коннектор недоступен — показываем кеш как есть
                        result = self._apply_precalc(cached)
                        self.data_ready.emit(result)
                else:
                    # Сразу показываем кеш (с precalc — тяжёлые вычисления здесь, не в GUI)
                    t0 = _time.monotonic()
                    result = self._apply_precalc(cached)
                    logger.debug(f"[DataLoader] precalc(cache) done in {_time.monotonic()-t0:.2f}s")
                    self.data_ready.emit(result)
                    # Догружаем только новые бары (от последнего бара кеша)
                    last_dt    = cached.index[-1].to_pydatetime()
                    delta_days = max(1, (datetime.now() - last_dt).days + 1)
                    fresh = self._load_history(days=delta_days)
                    if fresh is not None and not fresh.empty:
                        merged = chart_cache.merge(cached, fresh)
                        chart_cache.save(self.ticker, self.interval, merged)
                        if len(merged) != len(cached):
                            t0 = _time.monotonic()
                            result = self._apply_precalc(merged)
                            logger.debug(f"[DataLoader] precalc(merged) done in {_time.monotonic()-t0:.2f}s")
                            self.data_ready.emit(result)
                # Если коннектор недоступен — кеш уже показан, молча выходим
            else:
                t1 = _time.monotonic()
                df = self._load_history()
                logger.debug(f"[DataLoader] _load_history done in {_time.monotonic()-t1:.2f}s, df={'None' if df is None else len(df)}")
                if df is None or df.empty:
                    self.error.emit(
                        "Данные не получены — коннектор недоступен или нет истории.\n"
                        "Проверь подключение к брокеру."
                    )
                    return
                chart_cache.save(self.ticker, self.interval, df)
                t0 = _time.monotonic()
                result = self._apply_precalc(df)
                logger.debug(f"[DataLoader] precalc(fresh) done in {_time.monotonic()-t0:.2f}s")
                self.data_ready.emit(result)
        except Exception as e:
            logger.error(f"DataLoader error: {e}")
            self.error.emit(str(e))

    def _load_history(self, days: int = None) -> Optional[pd.DataFrame]:
        if self._cancelled:
            return None
        try:
            conn = connector_manager.get(self.connector_id)
            if not conn or not conn.is_connected():
                logger.debug(f"[DataLoader] коннектор {self.connector_id} недоступен")
                return None
            # Таймаут 30 сек — защита от зависания QUIK.
            # Внутренний daemon-поток позволяет прервать ожидание без блокировки QThread.
            import threading
            result_holder: list = []
            exc_holder:    list = []

            def _fetch():
                try:
                    r = conn.get_history(
                        ticker=self.ticker, board=self.board,
                        period=self.interval, days=days or self.days,
                    )
                    result_holder.append(r)
                except Exception as ex:
                    exc_holder.append(ex)

            t = threading.Thread(target=_fetch, daemon=True)
            t.start()
            t.join(timeout=120)
            if self._cancelled:
                return None
            if t.is_alive():
                logger.warning(f"[DataLoader] get_history timeout ({self.connector_id})")
                return None
            if exc_holder:
                raise exc_holder[0]
            return result_holder[0] if result_holder else None
        except Exception as e:
            logger.debug(f"[DataLoader] {self.connector_id}: {e}")
            return None


# ─────────────────────────────────────────────
# Кастомные графические элементы pyqtgraph
# ─────────────────────────────────────────────

class CandlestickItem(pg.GraphicsObject):
    """Свечной график. Рисует тела и фитили через QPainter за один проход.
    Использует числовой индекс по X (без пробелов выходных).
    """

    def __init__(self):
        super().__init__()
        self._data    = None
        self._picture = None
        self._bounds  = None

    def set_data(self, xs, opens, highs, lows, closes):
        self._data    = (xs, opens, highs, lows, closes)
        self._picture = None
        self._bounds  = None
        self._generate_picture()
        self.prepareGeometryChange()
        self.update()

    def update_last(self, high: float, low: float, close: float):
        """Обновляет только последнюю свечу без полной перерисовки всего массива.
        Вызывается с каждым тиком — меняет High/Low/Close последнего бара.
        """
        if self._data is None:
            return
        xs, opens, highs, lows, closes = self._data
        if len(xs) == 0:
            return
        highs  = highs.copy();  highs[-1]  = max(highs[-1],  high)
        lows   = lows.copy();   lows[-1]   = min(lows[-1],   low)
        closes = closes.copy(); closes[-1] = close
        self._data    = (xs, opens, highs, lows, closes)
        self._picture = None
        self._bounds  = None
        self._generate_picture()
        self.prepareGeometryChange()
        self.update()

    def _generate_picture(self):
        if self._data is None:
            return
        xs, opens, highs, lows, closes = self._data
        n = len(xs)
        if n == 0:
            return
        try:
            from PyQt6.QtGui import QPicture
            pic = QPicture()
            p   = QPainter(pic)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

            w = 0.4

            pen_up   = QPen(QColor(CANDLE_UP),   1); pen_up.setCosmetic(True)
            pen_down = QPen(QColor(CANDLE_DOWN), 1); pen_down.setCosmetic(True)
            brush_up   = QBrush(QColor(CANDLE_UP))
            brush_down = QBrush(QColor(CANDLE_DOWN))

            for i in range(n):
                x, o, h, l, c = (float(xs[i]), float(opens[i]),
                                  float(highs[i]), float(lows[i]), float(closes[i]))
                is_up = c >= o
                p.setPen(pen_up   if is_up else pen_down)
                p.setBrush(brush_up if is_up else brush_down)
                # Фитиль
                p.drawLine(
                    pg.QtCore.QPointF(x, l),
                    pg.QtCore.QPointF(x, h),
                )
                # Тело
                body_top    = max(o, c)
                body_bottom = min(o, c)
                body_h = max(body_top - body_bottom, 1e-10)
                p.drawRect(pg.QtCore.QRectF(x - w / 2, body_bottom, w, body_h))

            p.end()
            self._picture = pic

            self._bounds = pg.QtCore.QRectF(
                float(xs[0]) - w,
                float(lows.min()),
                float(xs[-1] - xs[0]) + 2 * w,
                float(highs.max() - lows.min()),
            )
        except Exception as e:
            logger.error(f"[CandlestickItem] _generate_picture error: {e}")

    def paint(self, p, *args):
        if self._picture is not None:
            self._picture.play(p)

    def boundingRect(self):
        if self._bounds is None:
            return pg.QtCore.QRectF()
        return self._bounds


class VolumeItem(pg.GraphicsObject):
    """Гистограмма объёма. Цвет баров совпадает с направлением свечи."""

    def __init__(self):
        super().__init__()
        self._data    = None
        self._picture = None
        self._bounds  = None

    def set_data(self, xs, vols, closes, opens):
        self._data    = (xs, vols, closes, opens)
        self._picture = None
        self._bounds  = None
        self._generate_picture()
        self.prepareGeometryChange()
        self.update()

    def update_last(self, vol: float, close: float, open_: float):
        """Обновляет только последний бар объёма без полной перерисовки."""
        if self._data is None:
            return
        xs, vols, closes, opens = self._data
        if len(xs) == 0:
            return
        vols   = vols.copy();   vols[-1]   = vol
        closes = closes.copy(); closes[-1] = close
        opens  = opens.copy();  opens[-1]  = open_
        self._data    = (xs, vols, closes, opens)
        self._picture = None
        self._bounds  = None
        self._generate_picture()
        self.prepareGeometryChange()
        self.update()

    def _generate_picture(self):
        if self._data is None:
            return
        xs, vols, closes, opens = self._data
        n = len(xs)
        if n == 0:
            return
        try:
            from PyQt6.QtGui import QPicture
            pic = QPicture()
            p   = QPainter(pic)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

            w = 0.4
            pen_up   = QPen(QColor(CANDLE_UP),   1); pen_up.setCosmetic(True)
            pen_down = QPen(QColor(CANDLE_DOWN), 1); pen_down.setCosmetic(True)
            brush_up   = QBrush(QColor(CANDLE_UP))
            brush_down = QBrush(QColor(CANDLE_DOWN))

            for i in range(n):
                x, v, c, o = (float(xs[i]), float(vols[i]),
                              float(closes[i]), float(opens[i]))
                is_up = c >= o
                p.setPen(pen_up   if is_up else pen_down)
                p.setBrush(brush_up if is_up else brush_down)
                p.drawRect(pg.QtCore.QRectF(x - w / 2, 0, w, v))

            p.end()
            self._picture = pic

            self._bounds = pg.QtCore.QRectF(
                float(xs[0]) - w, 0,
                float(xs[-1] - xs[0]) + 2 * w,
                float(vols.max()) * 1.05,
            )
        except Exception as e:
            logger.error(f"[VolumeItem] _generate_picture error: {e}")

    def paint(self, p, *args):
        if self._picture is not None:
            self._picture.play(p)

    def boundingRect(self):
        if self._bounds is None:
            return pg.QtCore.QRectF()
        return self._bounds


# ─────────────────────────────────────────────
# Кастомная ось X с датами
# ─────────────────────────────────────────────

class DateAxisItem(pg.AxisItem):
    """Ось X: числовой индекс -> дата/время.
    dates — список datetime, соответствующий индексам 0..n-1.
    """

    def __init__(self, dates: list, **kwargs):
        super().__init__(orientation="bottom", **kwargs)
        self._dates = dates
        self.setStyle(tickTextOffset=4)

    def update_dates(self, dates: list):
        self._dates = dates

    def tickStrings(self, values, scale, spacing):
        result = []
        n = len(self._dates)
        for v in values:
            i = int(round(v))
            if 0 <= i < n:
                dt = self._dates[i]
                if spacing >= 1440:
                    result.append(dt.strftime("%d.%m"))
                else:
                    result.append(dt.strftime("%d.%m\n%H:%M"))
            else:
                result.append("")
        return result


# ─────────────────────────────────────────────
# Правая ось цены с ПКМ-масштабированием по Y
# ─────────────────────────────────────────────

class RightPriceAxis(pg.AxisItem):
    """Правая ось цены с поддержкой ПКМ-drag для масштабирования по Y.

    Роль: заменяет стандартную правую ось plot_main.
    Поведение:
      - ЛКМ-drag по оси — стандартный pan по Y (pyqtgraph default)
      - ПКМ-drag вверх/вниз по оси — zoom по Y (уменьшение/увеличение высоты баров)
      - Зажатая ПКМ + движение вверх → zoom in (бары выше)
      - Зажатая ПКМ + движение вниз → zoom out (бары ниже)

    Вызывается из: _draw_chart_impl через axisItems={"right": RightPriceAxis()}
    """

    def __init__(self, **kwargs):
        super().__init__(orientation="right", **kwargs)
        self._rclick_start_y: Optional[float] = None   # Y-координата начала ПКМ-drag
        self._rclick_vrange: Optional[tuple]  = None   # (vmin, vmax) в момент нажатия

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.RightButton:
            self._rclick_start_y = ev.pos().y()
            vb = self.linkedView()
            if vb is not None:
                r = vb.viewRange()
                self._rclick_vrange = (r[1][0], r[1][1])  # (yMin, yMax)
            ev.accept()
        else:
            super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if (ev.buttons() & Qt.MouseButton.RightButton) and self._rclick_start_y is not None:
            vb = self.linkedView()
            if vb is None or self._rclick_vrange is None:
                return
            dy = ev.pos().y() - self._rclick_start_y   # пиксели смещения
            # Чувствительность: 1px = 0.5% изменения диапазона
            factor = 1.0 + dy * 0.005
            factor = max(0.1, min(factor, 10.0))        # ограничение 10x..0.1x
            ymin, ymax = self._rclick_vrange
            center = (ymin + ymax) / 2.0
            half   = (ymax - ymin) / 2.0 * factor
            vb.setYRange(center - half, center + half, padding=0)
            ev.accept()
        else:
            super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.RightButton:
            self._rclick_start_y = None
            self._rclick_vrange  = None
            ev.accept()
        else:
            super().mouseReleaseEvent(ev)

    def contextMenuEvent(self, ev):
        # Подавляем стандартное контекстное меню pyqtgraph по ПКМ на оси
        ev.accept()


# ─────────────────────────────────────────────
# Crosshair + tooltip
# ─────────────────────────────────────────────

class CrosshairOverlay:
    """Перекрестие на pyqtgraph PlotWidget.
    Рисует вертикальную/горизонтальную линии через InfiniteLine,
    обновляет tooltip-метку внизу окна.
    """

    def __init__(self, plot_main: pg.PlotItem, plot_vol: pg.PlotItem,
                 tooltip_lbl: QLabel, df: pd.DataFrame,
                 pairs: list, dates: list, decimals: int = 2):
        self.plot_main = plot_main
        self.plot_vol  = plot_vol
        self.tooltip   = tooltip_lbl
        self.df        = df
        self.pairs     = pairs
        self.dates     = dates
        self.decimals  = decimals
        self._order_index = self._build_order_index()

        kw = dict(pen=_mk_pen(CROSS_COLOR, 0.8, Qt.PenStyle.DashLine), movable=False)

        self._vline_main = pg.InfiniteLine(angle=90, **kw)
        self._hline_main = pg.InfiniteLine(angle=0,  **kw)
        self._vline_vol  = pg.InfiniteLine(angle=90, **kw)

        plot_main.addItem(self._vline_main, ignoreBounds=True)
        plot_main.addItem(self._hline_main, ignoreBounds=True)
        plot_vol.addItem(self._vline_vol,   ignoreBounds=True)

        try:
            # anchor=(1, 0.5): правый край текста прижат к position=1.0 (правая граница вьюпорта).
            # Текст рисуется ЛЕВЕЕ правой оси — не обрезается панелью.
            self._price_label = pg.InfLineLabel(
                self._hline_main, text="", position=0.98,
                anchor=(1, 0.5),
                color=CROSS_COLOR,
                fill=pg.mkBrush(BG_MAIN),
            )
            # position=0.15: тултип времени на панели объёма, поднят выше нижней границы
            # чтобы не обрезался. anchor=(0.5, 1) — нижний центр текста на точке position.
            self._time_label = pg.InfLineLabel(
                self._vline_vol, text="", position=0.15,
                anchor=(0.5, 1),
                color=CROSS_COLOR,
                fill=pg.mkBrush(BG_MAIN),
            )
        except Exception as e:
            logger.warning(f"[CrosshairOverlay] InfLineLabel error: {e}")
            self._price_label = None
            self._time_label  = None

        self._set_visible(False)

        self._proxy_main = pg.SignalProxy(
            plot_main.scene().sigMouseMoved,
            rateLimit=30, slot=self._on_mouse_moved,
        )

    def _set_visible(self, v: bool):
        self._vline_main.setVisible(v)
        self._hline_main.setVisible(v)
        self._vline_vol.setVisible(v)

    def _build_order_index(self) -> dict:
        idx = {}
        if not self.pairs or self.df is None:
            return idx
        ts_arr = np.array([t.timestamp() for t in self.df.index.to_pydatetime()])
        n = len(ts_arr)
        delta = (ts_arr[1] - ts_arr[0]) if n > 1 else 3600
        for pair in self.pairs:
            for role in ("open", "close"):
                order = pair.get(role)
                if not order:
                    continue
                odt = _parse_trade_time(order)
                ots = odt.timestamp()
                i   = int(np.clip(np.searchsorted(ts_arr, ots), 0, n - 1))
                best = i
                if i > 0 and abs(ts_arr[i - 1] - ots) < abs(ts_arr[i] - ots):
                    best = i - 1
                if abs(ts_arr[best] - ots) <= delta:
                    closest = self.df.index[best].to_pydatetime()
                    idx.setdefault(closest, []).append({
                        "order": order, "pnl": pair["pnl"], "role": role,
                    })
        return idx

    def _on_mouse_moved(self, args):
        pos = args[0]
        vb  = self.plot_main.getViewBox()
        if not self.plot_main.sceneBoundingRect().contains(pos):
            self._set_visible(False)
            self.tooltip.setText("")
            return

        mp = vb.mapSceneToView(pos)
        xd, yd = mp.x(), mp.y()
        n = len(self.dates)
        if n == 0:
            return

        self._set_visible(True)
        self._vline_main.setPos(xd)
        self._hline_main.setPos(yd)
        self._vline_vol.setPos(xd)

        d = self.decimals
        if self._price_label is not None:
            self._price_label.setText(f" {yd:.{d}f} ")

        i   = int(np.clip(round(xd), 0, n - 1))
        xdt = self.dates[i]
        if self._time_label is not None:
            self._time_label.setText(xdt.strftime(" %d.%m %H:%M "))

        if 0 <= i < len(self.df):
            self._render_tooltip(self.df.iloc[i], xdt)

    def _render_tooltip(self, row, bdt: datetime):
        d   = self.decimals
        o, h, l, c = row.Open, row.High, row.Low, row.Close
        v   = int(row.Volume)
        chg = (c - o) / o * 100 if o else 0
        cc  = CANDLE_UP if c >= o else CANDLE_DOWN
        sgn = "+" if chg >= 0 else ""

        parts = [
            f"<span style='color:#6c7086'>{bdt.strftime('%d.%m.%Y %H:%M')}</span>",
            f"O:<b>{o:.{d}f}</b>",
            f"H:<b style='color:{CANDLE_UP}'>{h:.{d}f}</b>",
            f"L:<b style='color:{CANDLE_DOWN}'>{l:.{d}f}</b>",
            f"C:<b style='color:{cc}'>{c:.{d}f}</b>",
            f"<b style='color:{cc}'>{sgn}{chg:.2f}%</b>",
            f"<span style='color:#6c7086'>Vol:</span><b>{v:,}</b>",
        ]

        orders = self._order_index.get(bdt, [])
        for entry in orders:
            order = entry["order"]
            pnl   = entry["pnl"]
            role  = entry["role"]
            side  = order["side"].upper()
            qty   = order["quantity"]
            px    = order["price"]
            oc    = CANDLE_UP if side == "BUY" else CANDLE_DOWN
            arr   = "▲" if side == "BUY" else "▼"
            pstr  = ""
            if pnl is not None and role == "close":
                pc   = CANDLE_UP if pnl >= 0 else CANDLE_DOWN
                pstr = f"  П/У:<b style='color:{pc}'>{'+' if pnl>=0 else ''}{pnl:.2f}</b>"
            parts.append(
                f"<b style='color:{oc}'>{arr}{side} {qty}л@{px:.{d}f}</b>{pstr}"
            )

        self.tooltip.setText(
            "<span style='color:#313244'> | </span>".join(parts)
        )

    def disconnect(self):
        self._proxy_main.disconnect()
        try:
            self.plot_main.removeItem(self._vline_main)
            self.plot_main.removeItem(self._hline_main)
            self.plot_vol.removeItem(self._vline_vol)
        except Exception:
            pass


# ─────────────────────────────────────────────
# Главное окно
# ─────────────────────────────────────────────

class ChartWindow(QWidget):
    """Виджет графика стратегии на pyqtgraph.

    Может использоваться как самостоятельный QWidget (встраивается во вкладку QTabWidget)
    или как QDialog (обратная совместимость через ChartDialog-обёртку).

    Структура layout:
      header (QWidget)             — тикер, ТФ, кнопки
      progress (QProgressBar)      — индикатор загрузки
      _glw (GraphicsLayoutWidget)  — plot_main (свечи) + plot_vol (объём)
      lbl_tooltip (QLabel)         — OHLCV + сделки при наведении
      lbl_status (QLabel)          — источник данных, кол-во баров
    """

    def __init__(self, strategy_id: str, parent=None,
                 ticker_override: str = None, board_override: str = None):
        super().__init__(parent)
        self.sid    = strategy_id
        self.data   = get_strategy(strategy_id) or {}
        self.params = self.data.get("params", {})
        self.ticker = ticker_override or self.data.get("ticker") or self.params.get("ticker", "N/A")
        self.board  = board_override  or self.data.get("board")  or self.params.get("board", "TQBR")
        self.connector_id = self.data.get("connector", "finam")

        _TF_MAP = {"1": "1m", "5": "5m", "10": "15m", "15": "15m",
                   "30": "30m", "60": "1h", "240": "4h", "D": "1d"}
        agent_tf   = self.data.get("timeframe", "15")
        default_tf = _TF_MAP.get(agent_tf, "15m")

        self._df: Optional[pd.DataFrame] = None
        self._period    = "1 неделя"
        self._timeframe = default_tf
        self._crosshair: Optional[CrosshairOverlay] = None
        self._decimals  = 2

        name = self.data.get("name", strategy_id)
        self.setWindowTitle(f"График: {name} — {self.ticker}")
        self.setMinimumSize(960, 640)
        self.resize(1200, 760)
        self.setStyleSheet(STYLE_DIALOG)

        pg.setConfigOptions(antialias=False, useOpenGL=False)

        self._build_ui()
        self._load_data()

    # ── UI ────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)
        # bottom=26 — отступ под QStatusBar главного окна, чтобы lbl_tooltip/lbl_status
        # не перекрывались нижней панелью при встраивании во вкладку QTabWidget.
        layout.setContentsMargins(0, 0, 0, 26)
        layout.setSpacing(0)

        layout.addWidget(self._build_header())

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(3)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)

        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setBackground(BG_FIGURE)
        self._glw.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        layout.addWidget(self._glw, stretch=1)

        self.lbl_tooltip = QLabel("")
        self.lbl_tooltip.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_tooltip.setFixedHeight(26)
        self.lbl_tooltip.setStyleSheet(
            "background:#11111b; border-top:1px solid #313244;"
            "padding:2px 10px; font-size:12px;"
        )
        layout.addWidget(self.lbl_tooltip)

        self.lbl_status = QLabel("Загрузка...")
        self.lbl_status.setObjectName("lbl_status")
        self.lbl_status.setStyleSheet(
            "background:#11111b; padding:2px 10px; font-size:11px; color:#6c7086;"
        )
        layout.addWidget(self.lbl_status)

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setFixedHeight(52)
        header.setStyleSheet(
            "background-color:#181825; border-bottom:1px solid #313244;"
        )
        layout = QHBoxLayout(header)
        layout.setContentsMargins(14, 6, 14, 6)
        layout.setSpacing(6)

        lbl = QLabel(f"📈  {self.ticker}")
        lbl.setObjectName("lbl_title")
        layout.addWidget(lbl)

        sep = QLabel("|")
        sep.setStyleSheet("color:#45475a;")
        layout.addWidget(sep)

        lbl_tf = QLabel("ТФ:")
        lbl_tf.setStyleSheet("color:#6c7086; font-size:12px;")
        layout.addWidget(lbl_tf)

        self.cmb_tf = QComboBox()
        for tf in TIMEFRAMES:
            self.cmb_tf.addItem(tf)
        self.cmb_tf.setCurrentText(self._timeframe)
        self.cmb_tf.currentTextChanged.connect(self._on_timeframe)
        layout.addWidget(self.cmb_tf)

        sep2 = QLabel("|")
        sep2.setStyleSheet("color:#45475a;")
        layout.addWidget(sep2)

        layout.addStretch()

        hint = QLabel(
            "<span style='color:#45475a'>"
            "🖱 Колёсико — zoom  │  ЛКМ drag — прокрутка  │  ПКМ drag по шкале Цена — высота баров"
            "</span>"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setStyleSheet("font-size:11px;")
        layout.addWidget(hint)

        layout.addSpacing(8)

        legend = QLabel(
            "▲ BUY  ▼ SELL  "
            f"<span style='color:{CANDLE_UP}'>— профит</span>  "
            f"<span style='color:{CANDLE_DOWN}'>— луз</span>"
        )
        legend.setTextFormat(Qt.TextFormat.RichText)
        legend.setStyleSheet("color:#6c7086; font-size:11px;")
        layout.addWidget(legend)

        layout.addSpacing(8)

        btn_refresh = QPushButton("🔄 Обновить")
        btn_refresh.setObjectName("btn_refresh")
        btn_refresh.setFixedWidth(100)
        btn_refresh.clicked.connect(self._load_data)
        layout.addWidget(btn_refresh)

        return header

    # ── Загрузка ──────────────────────────────

    def _load_data(self):
        self.progress.setVisible(True)
        self.lbl_status.setText("⏳ Загрузка данных...")
        self.lbl_tooltip.setText("")

        # Останавливаем предыдущий загрузчик если ещё работает
        if hasattr(self, "_loader") and self._loader is not None:
            try:
                self._loader.data_ready.disconnect()
                self._loader.error.disconnect()
            except Exception:
                pass

        # Останавливаем таймер авто-обновления на время загрузки
        if hasattr(self, "_refresh_timer"):
            self._refresh_timer.stop()

        if self._crosshair:
            self._crosshair.disconnect()
            self._crosshair = None

        max_days = {
            "1m": 30, "5m": 90, "15m": 180, "30m": 365,
            "1h": 365, "4h": 730, "1d": 1825,
        }
        days = max_days.get(self._timeframe, 90)
        lookback_extra = self._get_lookback_days()
        self._display_days = days

        self._loader = DataLoader(
            ticker=self.ticker, board=self.board,
            days=days + lookback_extra, interval=self._timeframe,
            connector_id=self.connector_id,
            precalc_fn=self._apply_precalc,
        )
        self._loader.data_ready.connect(self._on_data_ready)
        self._loader.error.connect(self._on_data_error)
        self._loader.start()

    def _on_data_ready(self, df: pd.DataFrame):
        import threading as _threading
        import time as _time
        logger.debug(f"[Chart] _on_data_ready thread={_threading.current_thread().name} bars={len(df) if df is not None else 0}")
        t0 = _time.monotonic()
        self.progress.setVisible(False)
        try:
            if df is None or df.empty:
                self.lbl_status.setText("🔴 Получен пустой датафрейм")
                return

            try:
                conn = connector_manager.get(self.connector_id)
                src  = self.connector_id.upper() if (conn and conn.is_connected()) else "Демо"
            except Exception:
                src = "Демо"

            # precalc уже выполнен в DataLoader (QThread) — не блокируем GUI
            self._df       = df
            df_full        = df
            self._decimals = _price_decimals(df_full["Close"].values)
            self.lbl_status.setText(
                f"{src} | {self.ticker} | {self._timeframe} | "
                f"Баров: {len(df_full)} | "
                f"Обновлено: {datetime.now().strftime('%H:%M:%S')}  "
                f"│  Наведи мышь на свечу"
            )
            logger.debug(f"[Chart] _draw_chart start, elapsed={_time.monotonic()-t0:.2f}s")
            self._draw_chart(df_full)
            logger.debug(f"[Chart] _draw_chart done, elapsed={_time.monotonic()-t0:.2f}s")

            if not hasattr(self, "_refresh_timer"):
                self._refresh_timer = QTimer(self)
                self._refresh_timer.setSingleShot(True)
                self._refresh_timer.timeout.connect(self._auto_refresh)
            self._schedule_next_refresh()
        except Exception as e:
            logger.error(f"[Chart] _on_data_ready error: {e}", exc_info=True)
            self.lbl_status.setText(f"🔴 Ошибка обработки данных: {e}")

    def _on_data_error(self, err: str):
        self.progress.setVisible(False)
        self.lbl_status.setText(f"🔴 {err}")
        self._glw.clear()
        p = self._glw.addPlot()
        p.setMenuEnabled(False)
        p.hideAxis("bottom")
        p.hideAxis("left")
        p.setXRange(0, 1, padding=0)
        p.setYRange(0, 1, padding=0)
        txt = pg.TextItem(
            "Данные не получены\nПроверь подключение к брокеру",
            color=TICK_COLOR, anchor=(0.5, 0.5),
        )
        txt.setFont(pg.QtGui.QFont("Segoe UI", 16))
        p.addItem(txt)
        txt.setPos(0.5, 0.5)

    def _on_timeframe(self, tf: str):
        self._timeframe = tf
        self._load_data()

    def _schedule_next_refresh(self):
        tf_minutes = {
            "1m": 1, "5m": 5, "15m": 15, "30m": 30,
            "1h": 60, "4h": 240, "1d": 1440,
        }
        tf_min = tf_minutes.get(self._timeframe, 5)
        now    = datetime.now()
        total_min = now.hour * 60 + now.minute
        next_bar_min = (total_min // tf_min + 1) * tf_min
        seconds_to_next = (next_bar_min * 60) - (total_min * 60 + now.second)
        delay_ms = max((seconds_to_next + 3) * 1000, 5000)
        logger.debug(f"[Chart] Следующее обновление через {seconds_to_next}с ({self._timeframe})")
        self._refresh_timer.start(delay_ms)

    def _auto_refresh(self):
        try:
            conn = connector_manager.get(self.connector_id)
            if not conn or not conn.is_connected():
                return
        except Exception:
            return
        if hasattr(self, "_refresh_loader") and self._refresh_loader.isRunning():
            return
        max_days = {
            "1m": 30, "5m": 90, "15m": 180, "30m": 365,
            "1h": 365, "4h": 730, "1d": 1825,
        }
        days = max_days.get(self._timeframe, 90)
        lookback_extra = self._get_lookback_days()
        self._refresh_loader = DataLoader(
            ticker=self.ticker, board=self.board,
            days=days + lookback_extra, interval=self._timeframe,
            connector_id=self.connector_id,
            precalc_fn=self._apply_precalc,
        )
        self._refresh_loader.data_ready.connect(self._on_refresh_ready)
        self._refresh_loader.start()

    def _on_refresh_ready(self, df: pd.DataFrame):
        try:
            if df is None or df.empty:
                return
            old_len = len(self._df) if self._df is not None else 0
            # precalc уже выполнен в DataLoader (QThread) — не блокируем GUI
            self._df       = df
            df_full        = df
            self._decimals = _price_decimals(df_full["Close"].values)
            self.lbl_status.setText(
                f"{self.connector_id.upper()} | {self.ticker} | {self._timeframe} | "
                f"Баров: {len(df_full)} | "
                f"Обновлено: {datetime.now().strftime('%H:%M:%S')}  "
                f"│  Наведи мышь на свечу"
            )
            if len(df_full) != old_len:
                self._draw_chart(df_full)
            if hasattr(self, "_refresh_timer"):
                self._schedule_next_refresh()
        except Exception as e:
            logger.error(f"[Chart] _on_refresh_ready error: {e}", exc_info=True)

    # ── Отрисовка ─────────────────────────────

    def _draw_chart(self, df: pd.DataFrame):
        if df is None or df.empty:
            return
        try:
            self._draw_chart_impl(df)
        except Exception as e:
            logger.error(f"[Chart] _draw_chart error: {e}", exc_info=True)
            self.lbl_status.setText(f"🔴 Ошибка отрисовки: {e}")

    def _draw_chart_impl(self, df: pd.DataFrame):
        if self._crosshair:
            self._crosshair.disconnect()
            self._crosshair = None

        self._glw.clear()

        # Фильтруем невалидные бары: open/high/low/close должны быть > 0 и не NaN.
        # Мусорные бары (open=0, high=1000000 и т.п.) дают артефакты высотой в тысячи пунктов.
        opens_raw  = df["Open"].values.astype(float)
        highs_raw  = df["High"].values.astype(float)
        lows_raw   = df["Low"].values.astype(float)
        closes_raw = df["Close"].values.astype(float)
        valid_mask = (
            (opens_raw  > 0) & np.isfinite(opens_raw)  &
            (highs_raw  > 0) & np.isfinite(highs_raw)  &
            (lows_raw   > 0) & np.isfinite(lows_raw)   &
            (closes_raw > 0) & np.isfinite(closes_raw) &
            (highs_raw >= lows_raw) &  # high не может быть меньше low
            # Финам присылает "пустые" бары где open=close=high=low — фильтруем
            ~(
                (opens_raw == closes_raw) &
                (opens_raw == highs_raw)  &
                (opens_raw == lows_raw)
            )
        )
        df = df[valid_mask].copy()
        if df.empty:
            logger.warning("[Chart] все бары отфильтрованы как невалидные")
            return

        # Фильтруем аномальные бары по разбросу high-low.
        # Финам иногда присылает бары с high=1000000 или low=0 — они дают вертикальные линии.
        # Отсекаем бары где (high-low) > медиана * 10.
        hl_range = df["High"].values.astype(float) - df["Low"].values.astype(float)
        median_hl = np.median(hl_range[hl_range > 0])
        if median_hl > 0:
            outlier_mask = hl_range <= median_hl * 10
            n_removed = (~outlier_mask).sum()
            if n_removed > 0:
                logger.debug(f"[Chart] отфильтровано {n_removed} аномальных баров (high-low > медиана×10)")
            df = df[outlier_mask].copy()
        if df.empty:
            logger.warning("[Chart] все бары отфильтрованы как аномальные")
            return

        n      = len(df)
        xs     = np.arange(n, dtype=float)
        dates  = list(df.index.to_pydatetime())
        opens  = df["Open"].values.astype(float)
        highs  = df["High"].values.astype(float)
        lows   = df["Low"].values.astype(float)
        closes = df["Close"].values.astype(float)
        vols   = df["Volume"].values.astype(float)

        date_axis_main = DateAxisItem(dates)
        date_axis_vol  = DateAxisItem(dates)

        # PlotItem: свечи
        # setBackground — метод GraphicsLayoutWidget, не PlotItem; фон задаётся через _glw
        # RightPriceAxis — кастомная ось с ПКМ-drag для масштабирования по Y
        _right_axis = RightPriceAxis()
        _right_axis.setTextPen(pg.mkPen(TICK_COLOR))
        _right_axis.setPen(pg.mkPen(GRID_COLOR))
        self._plot_main = self._glw.addPlot(
            row=0, col=0,
            axisItems={"bottom": date_axis_main, "right": _right_axis},
        )
        self._plot_main.setMenuEnabled(False)
        self._plot_main.showGrid(x=True, y=True, alpha=0.25)
        self._plot_main.getAxis("bottom").setStyle(showValues=False)
        self._plot_main.getAxis("left").hide()
        self._plot_main.showAxis("right")
        # +38px (~1 см при 96 DPI) к стандартной ширине правой оси (~37px)
        self._plot_main.getAxis("right").setWidth(75)

        # PlotItem: объём
        self._glw.nextRow()
        self._plot_vol = self._glw.addPlot(
            row=1, col=0,
            axisItems={"bottom": date_axis_vol},
        )
        self._plot_vol.setMenuEnabled(False)
        self._plot_vol.showGrid(x=True, y=True, alpha=0.2)
        self._plot_vol.getAxis("left").hide()
        self._plot_vol.showAxis("right")
        self._plot_vol.getAxis("right").setTextPen(pg.mkPen(TICK_COLOR))
        self._plot_vol.getAxis("right").setPen(pg.mkPen(GRID_COLOR))
        self._plot_vol.getAxis("right").setWidth(75)
        self._plot_vol.setXLink(self._plot_main)

        # Высота: main 75%, vol 25%
        self._glw.ci.layout.setRowStretchFactor(0, 3)
        self._glw.ci.layout.setRowStretchFactor(1, 1)

        # Свечи
        self._candle_item = CandlestickItem()
        self._candle_item.set_data(xs, opens, highs, lows, closes)
        self._plot_main.addItem(self._candle_item)

        # Объём
        self._vol_item = VolumeItem()
        self._vol_item.set_data(xs, vols, closes, opens)
        self._plot_vol.addItem(self._vol_item)

        # Вертикальные разделители дней
        day_seen = set()
        for i, dt in enumerate(dates):
            day = dt.date()
            if day not in day_seen:
                day_seen.add(day)
                for plot in (self._plot_main, self._plot_vol):
                    vl = pg.InfiniteLine(
                        pos=i, angle=90,
                        pen=_mk_pen("#313244", 0.5),
                    )
                    plot.addItem(vl, ignoreBounds=True)

        # Диапазон просмотра — восстанавливаем пользовательский viewport если он был задан,
        # иначе устанавливаем дефолтный (весь диапазон данных).
        _saved_xrange = getattr(self, "_saved_xrange", None)
        _saved_yrange = getattr(self, "_saved_yrange", None)
        _user_zoomed  = getattr(self, "_user_zoomed", False)

        if _user_zoomed and _saved_xrange is not None and _saved_yrange is not None:
            self._plot_main.setXRange(*_saved_xrange, padding=0)
            self._plot_main.setYRange(*_saved_yrange, padding=0)
        else:
            self._plot_main.setXRange(-2, n + 15, padding=0)
            self._plot_main.setYRange(lows.min() * 0.999, highs.max() * 1.001, padding=0)
        self._plot_vol.setYRange(0, vols.max() * 1.1, padding=0)

        # ── Ограничения pan/zoom ──────────────────────────────────────────
        # Запрещаем уйти далеко за пределы данных по X и Y.
        # xMin/xMax — небольшой запас за края данных.
        # yMin/yMax — 50% запас от реального диапазона цен (не даёт улететь в пустоту).
        price_range = highs.max() - lows.min()
        y_pad = price_range * 0.5
        self._plot_main.getViewBox().setLimits(
            xMin=-n * 0.1,
            xMax=n * 1.1,
            yMin=lows.min()  - y_pad,
            yMax=highs.max() + y_pad,
        )
        self._plot_vol.getViewBox().setLimits(
            xMin=-n * 0.1,
            xMax=n * 1.1,
            yMin=0,
            yMax=vols.max() * 3,
        )

        # ── Ограничение навигации ─────────────────────────────────────────
        # setLimits уже не даёт улететь далеко по X и Y (±10% / ±50%).
        # setMouseEnabled оставляем по умолчанию (x=True, y=True) —
        # иначе ПКМ-drag по полю графика перестаёт работать полностью.
        # Отключаем только контекстное меню ViewBox по ПКМ.
        self._plot_main.getViewBox().setMenuEnabled(False)
        self._plot_vol.getViewBox().setMenuEnabled(False)

        # Подписываемся на ручное изменение диапазона — сохраняем viewport
        # чтобы при следующей перерисовке (новый бар) не сбрасывать масштаб.
        def _on_range_changed_manually(vb, _range=None):
            try:
                xr = self._plot_main.getViewBox().viewRange()[0]
                yr = self._plot_main.getViewBox().viewRange()[1]
                self._saved_xrange = (xr[0], xr[1])
                self._saved_yrange = (yr[0], yr[1])
                self._user_zoomed  = True
            except Exception:
                pass

        self._plot_main.getViewBox().sigRangeChangedManually.connect(_on_range_changed_manually)

        # Ордера
        pairs = [p for p in get_order_pairs(self.sid)
                 if p["open"].get("ticker") == self.ticker]
        if pairs:
            self._draw_order_pairs(self._plot_main, df, pairs, dates)

        # Индикаторы стратегии
        self._draw_indicators(self._plot_main, df, xs)

        # Линия текущей цены
        self._start_price_ticker()

        # Crosshair
        self._crosshair = CrosshairOverlay(
            plot_main=self._plot_main,
            plot_vol=self._plot_vol,
            tooltip_lbl=self.lbl_tooltip,
            df=df,
            pairs=pairs,
            dates=dates,
            decimals=self._decimals,
        )

        self._xs    = xs
        self._dates = dates

    def _draw_order_pairs(self, plot, df: pd.DataFrame, pairs: list, dates: list):
        n = len(dates)
        if n == 0:
            return
        ts_arr = np.array([d.timestamp() for d in dates])
        tf_sec = (ts_arr[1] - ts_arr[0]) if n > 1 else 60

        def _find_bar_idx(dt_target):
            ts = dt_target.timestamp()
            i  = int(np.searchsorted(ts_arr, ts, side="right")) - 1
            i  = int(np.clip(i, 0, n - 1))
            if abs(ts_arr[i] - ts) <= tf_sec * 2:
                return i
            return None

        d = self._decimals
        for pair in pairs:
            open_ord  = pair["open"]
            close_ord = pair["close"]
            pnl       = pair["pnl"]
            is_long   = pair["is_long"]

            open_dt = _parse_trade_time(open_ord)
            open_px = open_ord["price"]
            oi = _find_bar_idx(open_dt)

            if (open_px == 0 or open_px is None) and oi is not None:
                open_px = float(df.iloc[oi]["Close"])

            if oi is not None:
                color = CANDLE_UP if is_long else CANDLE_DOWN
                sym   = "t1" if is_long else "t"
                scatter_open = pg.ScatterPlotItem(
                    [oi], [open_px],
                    symbol=sym, size=12,
                    pen=_mk_pen(BG_FIGURE, 1),
                    brush=pg.mkBrush(color),
                )
                plot.addItem(scatter_open)

                lbl_text = f"{'BUY' if is_long else 'SELL'}\n{open_px:.{d}f}"
                lbl = pg.TextItem(lbl_text, color=color,
                                  anchor=(0.5, 1.5 if is_long else -0.5))
                lbl.setPos(oi, open_px)
                lbl.setFont(pg.QtGui.QFont("Segoe UI", 7))
                plot.addItem(lbl)

            if close_ord:
                close_dt = _parse_trade_time(close_ord)
                close_px = close_ord["price"]
                commission = float(self.params.get("commission", 0))
                qty_pair   = min(open_ord["quantity"], close_ord["quantity"])
                comm_total = commission * qty_pair * 2
                pnl_net    = (pnl - comm_total) if pnl is not None else None
                # Цвет линии/маркера — по сырому PnL (прибыльная/убыточная по цене),
                # метка показывает net PnL уже с учётом комиссии
                lc = CANDLE_UP if (pnl is not None and pnl >= 0) else CANDLE_DOWN

                ci = _find_bar_idx(close_dt)
                if ci is not None:
                    scatter_close = pg.ScatterPlotItem(
                        [ci], [close_px],
                        symbol="x", size=12,
                        pen=_mk_pen(lc, 2),
                        brush=pg.mkBrush(lc),
                    )
                    plot.addItem(scatter_close)

                    oi_line = oi if oi is not None else ci
                    line = pg.PlotDataItem(
                        [oi_line, ci], [open_px, close_px],
                        pen=_mk_pen(lc, 1.2, Qt.PenStyle.DashLine),
                    )
                    plot.addItem(line)

                    if pnl_net is not None:
                        mx = (oi_line + ci) / 2
                        my = (open_px + close_px) / 2
                        ps = f"{'+' if pnl_net >= 0 else ''}{pnl_net:.2f}"
                        pnl_lbl = pg.TextItem(
                            ps, color=lc, anchor=(0.5, 1.0),
                            fill=pg.mkBrush(BG_FIGURE),
                            border=_mk_pen(lc, 0.8),
                        )
                        pnl_lbl.setPos(mx, my)
                        pnl_lbl.setFont(
                            pg.QtGui.QFont("Segoe UI", 8, pg.QtGui.QFont.Weight.Bold)
                        )
                        plot.addItem(pnl_lbl)

    def _draw_indicators(self, plot, df: pd.DataFrame, xs: np.ndarray):
        mod = self._load_strategy_module()
        if mod is None:
            return

        if hasattr(mod, "get_indicators"):
            indicators = mod.get_indicators()
        else:
            indicators = [
                {"col": c, "type": "line", "color": "#89b4fa", "label": c.lstrip("_")}
                for c in df.columns if c.startswith("_")
            ]

        legend_items = []
        for ind in indicators:
            col   = ind["col"]
            if col not in df.columns:
                continue
            vals  = df[col].values.astype(float)
            color = ind.get("color", "#89b4fa")
            lw    = ind.get("linewidth", 1.0)
            ls    = ind.get("linestyle", "-")
            kind  = ind.get("type", "line")
            label = ind.get("label", col.lstrip("_"))

            mask = ~np.isnan(vals)
            if not mask.any():
                continue

            qt_style = Qt.PenStyle.DashLine if ls == "--" else Qt.PenStyle.SolidLine
            pen = _mk_pen(color, lw, qt_style)

            if kind == "step":
                curve = pg.PlotDataItem(
                    xs[mask], vals[mask], pen=pen, stepMode="right",
                )
            else:
                curve = pg.PlotDataItem(xs[mask], vals[mask], pen=pen)

            plot.addItem(curve)
            legend_items.append((label, color))

        if legend_items:
            try:
                legend = plot.addLegend(
                    offset=(10, 10),
                    brush=pg.mkBrush(BG_MAIN + "cc"),
                    pen=_mk_pen("#313244"),
                )
                for label, color in legend_items:
                    dummy = pg.PlotDataItem(pen=_mk_pen(color, 1.5))
                    legend.addItem(dummy, label)
            except Exception as e:
                logger.warning(f"[Chart] addLegend: {e}")

    # ── Линия текущей цены + тик-обновление ──

    def _start_price_ticker(self):
        """Создаёт горизонтальную линию текущей цены.
        Реальное обновление идёт через _start_tick_update (тик-таймер 1 сек).
        Оставлен для совместимости — вызывается из _draw_chart_impl.
        """
        # Линия текущей цены: полупрозрачная (#f38ba880 = CANDLE_DOWN + 50% alpha),
        # пунктирная, не мешает восприятию свечей.
        _price_pen = QPen(QColor(CANDLE_DOWN + "80"), 1, Qt.PenStyle.DashLine)
        _price_pen.setCosmetic(True)
        self._price_line = pg.InfiniteLine(
            angle=0, movable=False,
            pen=_price_pen,
        )
        self._price_line.setVisible(False)
        self._plot_main.addItem(self._price_line, ignoreBounds=True)

        try:
            # anchor=(1, 0.5): правый край текста прижат к position=0.98 —
            # тултип рисуется ЛЕВЕЕ правой оси и не обрезается панелью.
            self._price_label_item = pg.InfLineLabel(
                self._price_line, text="", position=0.98,
                anchor=(1, 0.5),
                color=CANDLE_DOWN,
                fill=pg.mkBrush(BG_MAIN),
            )
        except Exception as e:
            logger.warning(f"[Chart] price InfLineLabel: {e}")
            self._price_label_item = None

        # Запускаем тик-таймер (подписка + 1 сек polling)
        self._start_tick_update()

    def _start_tick_update(self):
        """Подписывается на котировки и запускает тик-таймер 1 сек.

        Архитектура:
        - subscribe_quotes() — просит коннектор держать актуальный кеш котировок
          (для Финам: DLL callback обновляет _quotes dict без сетевых вызовов)
        - _tick_timer (1 сек) → _on_tick() читает кеш — нет блокирующих вызовов в GUI
        - Fallback: если коннектор не поддерживает подписку — get_last_price каждые 5 сек
        """
        self._tick_subscribed = False
        try:
            conn = connector_manager.get(self.connector_id)
            if conn and conn.is_connected() and hasattr(conn, "subscribe_quotes"):
                conn.subscribe_quotes(self.board, self.ticker)
                self._tick_subscribed = True
                logger.debug(f"[Chart] subscribe_quotes {self.ticker}")
        except Exception as e:
            logger.debug(f"[Chart] subscribe_quotes error: {e}")

        if not hasattr(self, "_tick_timer"):
            self._tick_timer = QTimer(self)
            self._tick_timer.setInterval(1_000)
            self._tick_timer.timeout.connect(self._on_tick)
        self._tick_timer.start()

        # Немедленно показываем последнюю известную цену из df
        if self._df is not None and not self._df.empty:
            try:
                price = float(self._df["Close"].iloc[-1])
                self._update_price_line(price)
            except Exception:
                pass

    def _on_tick(self):
        """Вызывается каждую секунду из GUI-потока.

        Логика получения цены:
        1. Финам: get_best_quote читает in-memory кэш (нет сетевых вызовов) → цена сразу.
        2. QUIK и другие без кэша: get_best_quote делает сетевые вызовы →
           выносим в daemon-поток через _fetch_price_async, чтобы не блокировать GUI.
        3. Если цена не получена — берём Close последней свечи из DataFrame.
        """
        price = None
        try:
            conn = connector_manager.get(self.connector_id)
            if conn and conn.is_connected():
                _get_q = getattr(conn, "get_best_quote", None) or getattr(conn, "get_quotes", None)
                if _get_q is not None:
                    # Проверяем: есть ли у коннектора in-memory кэш котировок
                    # (Финам хранит в self._quotes, QUIK делает сетевой вызов).
                    # Признак кэша — наличие атрибута _quotes на коннекторе.
                    has_cache = hasattr(conn, "_quotes")
                    if has_cache:
                        # Финам: читаем кэш без блокировки
                        q = _get_q(self.board, self.ticker)
                        if q:
                            price = q.get("last") or q.get("offer") or q.get("bid")
                    else:
                        # QUIK и др.: get_best_quote блокирует GUI → daemon-поток
                        self._fetch_price_async()
                        return
                else:
                    # Нет метода котировок — fallback через get_last_price
                    self._fetch_price_async()
                    return
        except Exception as e:
            logger.debug(f"[Chart] _on_tick: {e}")

        if price is None and self._df is not None and not self._df.empty:
            try:
                price = float(self._df["Close"].iloc[-1])
            except Exception:
                pass

        if price is not None:
            self._update_price_line(price)
            self._update_last_candle(price)

    def _fetch_price_async(self):
        """Получает цену в daemon-потоке (для QUIK и коннекторов без in-memory кэша).

        Использует get_best_quote (LAST → offer → bid), fallback — get_last_price.
        Защита от параллельных вызовов: если предыдущий поток ещё работает — пропускаем.
        """
        import threading
        if getattr(self, "_fetch_price_running", False):
            return
        self._fetch_price_running = True

        def _worker():
            price = None
            try:
                conn = connector_manager.get(self.connector_id)
                if conn and conn.is_connected():
                    # Пробуем get_best_quote (QUIK: LAST через get_param_ex)
                    _get_q = getattr(conn, "get_best_quote", None)
                    if _get_q is not None:
                        q = _get_q(self.board, self.ticker)
                        if q:
                            price = q.get("last") or q.get("offer") or q.get("bid")
                    # Fallback: get_last_price
                    if not price:
                        get_lp = getattr(conn, "get_last_price", None)
                        if get_lp is not None:
                            price = get_lp(self.ticker, self.board)
            except Exception as e:
                logger.debug(f"[Chart] _fetch_price_async: {e}")
            finally:
                self._fetch_price_running = False
            if price:
                from PyQt6.QtCore import QTimer as _QTimer
                _QTimer.singleShot(0, lambda: self._apply_tick_price(price))

        threading.Thread(target=_worker, daemon=True).start()

    # Оставляем алиас для обратной совместимости
    _fetch_last_price_async = _fetch_price_async

    def _apply_tick_price(self, price: float):
        """Применяет цену полученную асинхронно (вызывается в GUI-потоке)."""
        self._update_price_line(price)
        self._update_last_candle(price)

    def _update_last_candle(self, price: float):
        """Обновляет последнюю свечу текущим тиком без полной перерисовки графика.

        Меняет Close (и High/Low если цена вышла за пределы бара).
        Работает только если график уже отрисован (_candle_item существует).
        """
        if not hasattr(self, "_candle_item") or self._candle_item is None:
            return
        if self._df is None or self._df.empty:
            return
        try:
            last = self._df.iloc[-1]
            self._candle_item.update_last(
                high=max(float(last["High"]), price),
                low=min(float(last["Low"]), price),
                close=price,
            )
            if hasattr(self, "_vol_item") and self._vol_item is not None:
                self._vol_item.update_last(
                    vol=float(last["Volume"]),
                    close=price,
                    open_=float(last["Open"]),
                )
        except Exception as e:
            logger.debug(f"[Chart] _update_last_candle: {e}")

    def _update_price_line(self, price: float):
        if not hasattr(self, "_price_line"):
            return
        self._price_line.setPos(price)
        self._price_line.setVisible(True)
        if self._price_label_item is not None:
            self._price_label_item.setText(f" {price:.{self._decimals}f} ")

    # ── Стратегия ─────────────────────────────

    def _get_lookback_days(self) -> int:
        mod = self._load_strategy_module()
        if mod is None or not hasattr(mod, "get_lookback"):
            return 0
        try:
            lookback_bars = mod.get_lookback(self.params)
            bars_per_day  = {
                "1m": 840, "5m": 168, "15m": 56,
                "30m": 28, "1h": 14, "4h": 4, "1d": 1,
            }
            bpd = bars_per_day.get(self._timeframe, 56)
            return max(1, int(lookback_bars / bpd) + 2)
        except Exception:
            return 0

    def _load_strategy_module(self):
        file_path = self.data.get("file_path", "")
        if not file_path:
            return None
        try:
            import os
            module_name = os.path.splitext(os.path.basename(file_path))[0]
            return importlib.import_module(f"strategies.{module_name}")
        except Exception as e:
            logger.warning(f"[Chart] Не удалось загрузить модуль стратегии: {e}")
            return None

    def _apply_precalc(self, df: pd.DataFrame) -> pd.DataFrame:
        """Вызывает on_precalc стратегии на df с lowercase-колонками."""
        mod = self._load_strategy_module()
        if mod is None or not hasattr(mod, "on_precalc"):
            return df
        try:
            df_strat = df.rename(columns={
                "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "vol",
            })
            # Убеждаемся что индекс DatetimeIndex перед strftime
            if not isinstance(df_strat.index, pd.DatetimeIndex):
                df_strat.index = pd.to_datetime(df_strat.index)
            if "date_int" not in df_strat.columns:
                df_strat["date_int"] = df_strat.index.strftime("%y%m%d").astype(int)
            if "time_min" not in df_strat.columns:
                df_strat["time_min"] = df_strat.index.hour * 60 + df_strat.index.minute
            if "weekday" not in df_strat.columns:
                df_strat["weekday"] = df_strat.index.dayofweek + 1

            df_strat = mod.on_precalc(df_strat, self.params)

            for col in df_strat.columns:
                if col.startswith("_"):
                    # Используем выравнивание по индексу для избежания NaN при несовпадающих индексах
                    # (например, после merge в daytrend.py)
                    df.loc[df_strat.index, col] = df_strat[col].values
        except Exception as e:
            logger.warning(f"[Chart] on_precalc ошибка: {e}", exc_info=True)
        return df

    # ── Закрытие ──────────────────────────────

    def closeEvent(self, event):
        # Тик-таймер (1 сек) — останавливаем первым
        if hasattr(self, "_tick_timer"):
            self._tick_timer.stop()
        # Отписка от котировок
        if getattr(self, "_tick_subscribed", False):
            try:
                conn = connector_manager.get(self.connector_id)
                if conn and hasattr(conn, "unsubscribe_quotes"):
                    conn.unsubscribe_quotes(self.board, self.ticker)
                    logger.debug(f"[Chart] unsubscribe_quotes {self.ticker}")
            except Exception as e:
                logger.debug(f"[Chart] unsubscribe_quotes error: {e}")
            self._tick_subscribed = False
        if hasattr(self, "_price_timer"):
            self._price_timer.stop()
        if hasattr(self, "_refresh_timer"):
            self._refresh_timer.stop()
        if self._crosshair:
            self._crosshair.disconnect()
        # Отменяем активные загрузчики — иначе join(30) держит закрытие окна
        for attr in ("_loader", "_refresh_loader"):
            loader = getattr(self, attr, None)
            if loader is not None:
                loader._cancelled = True
        event.accept()
