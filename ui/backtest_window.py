# ui/backtest_window.py

import inspect
from dataclasses import fields, MISSING
from pathlib import Path

from loguru import logger
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QFileDialog, QSpinBox,
    QDoubleSpinBox, QGroupBox, QProgressBar,
    QMessageBox, QFrame, QScrollArea, QWidget, QComboBox,
)

from core.backtest_engine import BacktestEngine, BacktestResult
from core.txt_loader import TXTLoader
from config.settings import STRATEGIES_DIR



# ── Worker ───────────────────────────────────────────────────────────────────

class BacktestWorker(QThread):
    finished = pyqtSignal(object)
    error    = pyqtSignal(str)

    def __init__(self, module, filepath: str, connector_id: str = "finam", board: str = "TQBR"):
        super().__init__()
        self._module   = module
        self._filepath = filepath
        self._connector_id = connector_id
        self._board = board
        self._stopped  = False

    def stop(self):
        self._stopped = True

    def run(self):
        try:
            engine = BacktestEngine(TXTLoader())
            result = engine.run(
                self._module, self._filepath, 
                connector_id=self._connector_id,
                board=self._board,
                stop_flag=lambda: self._stopped
            )
            if not self._stopped:
                self.finished.emit(result)
        except InterruptedError:
            pass
        except Exception as e:
            if not self._stopped:
                logger.exception("Ошибка в BacktestWorker")
                self.error.emit(str(e))

# ── Главное окно ─────────────────────────────────────────────────────────────

class BacktestWindow(QDialog):

    STYLE = """
        QDialog {
            background-color: #1e1e2e;
            color: #cdd6f4;
            font-family: 'Segoe UI', sans-serif;
            font-size: 13px;
        }
        QGroupBox {
            border: 1px solid #45475a;
            border-radius: 6px;
            margin-top: 10px;
            padding: 10px;
            color: #cdd6f4;
            font-weight: bold;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 4px;
            color: #89b4fa;
        }
        QLabel { color: #cdd6f4; }
        QLabel#file_label {
            color: #a6adc8;
            font-size: 11px;
            padding: 6px 8px;
            background: #181825;
            border: 1px solid #45475a;
            border-radius: 4px;
        }
        QSpinBox, QDoubleSpinBox {
            background: #181825;
            color: #cdd6f4;
            border: 1px solid #45475a;
            border-radius: 4px;
            padding: 4px 8px;
        }
        QSpinBox:focus, QDoubleSpinBox:focus { border-color: #89b4fa; }
        QPushButton {
            border-radius: 5px;
            padding: 7px 18px;
            font-weight: bold;
        }
        QPushButton#btn_file, QPushButton#btn_strategy {
            background: #313244;
            color: #cdd6f4;
            border: 1px solid #45475a;
        }
        QPushButton#btn_file:hover,
        QPushButton#btn_strategy:hover  { background: #45475a; }
        QPushButton#btn_run {
            background: #89b4fa;
            color: #1e1e2e;
            border: none;
        }
        QPushButton#btn_run:hover    { background: #b4befe; }
        QPushButton#btn_run:disabled { background: #45475a; color: #6c7086; }
        QPushButton#btn_close {
            background: #313244;
            color: #cdd6f4;
            border: 1px solid #45475a;
        }
        QPushButton#btn_close:hover { background: #45475a; }
        QProgressBar {
            background: #181825;
            border: 1px solid #45475a;
            border-radius: 4px;
            text-align: center;
            color: #cdd6f4;
            height: 18px;
        }
        QProgressBar::chunk { background: #89b4fa; border-radius: 3px; }
        QFrame#divider { background: #45475a; max-height: 1px; }
        QScrollArea { border: none; background: transparent; }
    """

    def __init__(self, strategy_id: str | None = None,
                 strategy_file_path: str | None = None,
                 connector_id: str = "finam",
                 board: str = "TQBR",
                 parent=None):
        super().__init__(parent)
        self._strategy_id = strategy_id
        self._strategy_file_path = strategy_file_path
        self._connector_id = connector_id
        self._board = board
        self._data_filepath: str | None = None
        self._strategy_module = None
        self._param_widgets: dict = {}
        self._worker: BacktestWorker | None = None

        self.setWindowTitle("Бэктест стратегии")
        self.setMinimumWidth(500)
        self.setModal(True)
        self.setStyleSheet(self.STYLE)
        self._build_ui()

        if strategy_id or strategy_file_path:
            self._autoload_strategy(strategy_id, strategy_file_path)  # ← оба аргумента

    # ── Построение UI ────────────────────────────────────────────────────────

    def _build_ui(self):
        self._root = QVBoxLayout(self)
        self._root.setSpacing(12)
        self._root.setContentsMargins(16, 16, 16, 16)

        self._root.addWidget(self._build_strategy_group())
        self._root.addWidget(self._build_file_group())

        # Контейнер для динамических параметров
        self._params_box = QGroupBox("Параметры стратегии")
        self._params_box.setVisible(False)
        self._params_layout = QGridLayout(self._params_box)
        self._params_layout.setColumnStretch(1, 1)
        self._params_layout.setHorizontalSpacing(16)
        self._params_layout.setVerticalSpacing(8)
        self._root.addWidget(self._params_box)

        self._root.addWidget(self._build_divider())
        self._root.addWidget(self._build_progress())
        self._root.addLayout(self._build_buttons())

    def _build_strategy_group(self) -> QGroupBox:
        box = QGroupBox("Файл стратегии")
        lay = QVBoxLayout(box)

        self._strategy_label = QLabel("Стратегия не выбрана")
        self._strategy_label.setObjectName("file_label")
        self._strategy_label.setWordWrap(True)

        btn = QPushButton("📂  Выбрать стратегию (.py)...")
        btn.setObjectName("btn_strategy")
        btn.clicked.connect(self._choose_strategy)

        lay.addWidget(self._strategy_label)
        lay.addWidget(btn)
        return box

    def _build_file_group(self) -> QGroupBox:
        box = QGroupBox("Файл котировок")
        lay = QVBoxLayout(box)

        self._file_label = QLabel("Файл не выбран")
        self._file_label.setObjectName("file_label")
        self._file_label.setWordWrap(True)

        btn = QPushButton("📂  Выбрать котировки (.txt / .csv)...")
        btn.setObjectName("btn_file")
        btn.clicked.connect(self._choose_file)

        lay.addWidget(self._file_label)
        lay.addWidget(btn)
        return box

    def _build_divider(self) -> QFrame:
        f = QFrame()
        f.setObjectName("divider")
        f.setFrameShape(QFrame.Shape.HLine)
        return f

    def _build_progress(self) -> QProgressBar:
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)
        return self._progress

    def _build_buttons(self) -> QHBoxLayout:
        lay = QHBoxLayout()
        lay.setSpacing(8)

        self._btn_run = QPushButton("▶  Запустить бэктест")
        self._btn_run.setObjectName("btn_run")
        self._btn_run.setEnabled(False)
        self._btn_run.clicked.connect(self._run_backtest)

        btn_close = QPushButton("Закрыть")
        btn_close.setObjectName("btn_close")
        btn_close.clicked.connect(self.reject)

        lay.addStretch()
        lay.addWidget(btn_close)
        lay.addWidget(self._btn_run)
        return lay

    # ── Динамические параметры ───────────────────────────────────────────────

    def _build_params_widgets(self, params_schema: dict):
        """
        Строит виджеты из словаря get_params() стратегии.
        Класс: BacktestWindow
        """
        while self._params_layout.count():
            item = self._params_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._param_widgets.clear()

        SKIP = {"ticker", "instruments"}  # нередактируемые/сложные в бэктесте

        for row_idx, (fname, meta) in enumerate(params_schema.items()):
            if fname in SKIP:
                continue

            ftype = meta.get("type", "float")
            default = meta.get("default", 0)
            label = meta.get("label", fname)
            desc = meta.get("description", "")

            lbl = QLabel(label)
            self._params_layout.addWidget(lbl, row_idx, 0)

            if ftype == "int":
                widget = QSpinBox()
                widget.setRange(
                    meta.get("min", -99999),
                    meta.get("max", 99999)
                )
                widget.setValue(int(default))
            elif ftype in ("float", "commission"):
                widget = QDoubleSpinBox()
                widget.setRange(
                    float(meta.get("min", -99999)),
                    float(meta.get("max", 99999))
                )
                widget.setDecimals(6)
                widget.setSingleStep(0.001)
                if default == "auto":
                    widget.setValue(0.0)
                else:
                    widget.setValue(float(default))
            elif ftype == "time":
                widget = QSpinBox()
                widget.setRange(0, 1439)
                widget.setValue(int(default))
            elif ftype in ("str", "select"):
                widget = QComboBox()
                if meta.get("options"):
                    for idx, option in enumerate(meta.get("options", [])):
                        label_text = meta.get("labels", meta.get("options", []))[idx]
                        widget.addItem(str(label_text), option)
                    current_idx = widget.findData(default)
                    if current_idx >= 0:
                        widget.setCurrentIndex(current_idx)
                else:
                    widget.addItem(str(default), default)
            else:
                logger.debug(
                    f"[BacktestWindow] Параметр {fname} type={ftype} пропущен в UI бэктеста"
                )
                continue

            self._params_layout.addWidget(widget, row_idx, 1)
            self._param_widgets[fname] = widget

            if desc:
                hint = QLabel(desc)
                hint.setStyleSheet("color: #6c7086; font-size: 11px;")
                self._params_layout.addWidget(hint, row_idx, 2)

        self._params_box.setVisible(len(self._param_widgets) > 0)
        self.adjustSize()

    @staticmethod
    def _param_hint(name: str, default) -> str:
        hints = {
            "period": "баров (Highest/Lowest)",
            "otstup": "смещение назад",
            "time_open": f"мин от полуночи  ({int(default) // 60:02d}:{int(default) % 60:02d})",
            "time_close": f"мин от полуночи  ({int(default) // 60:02d}:{int(default) % 60:02d})",
            "commission": "руб. на контракт",
        }
        return hints.get(name, "")

    # ── Загрузка стратегии ───────────────────────────────────────────────────

    def _choose_strategy(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Выбери файл стратегии",
            "strategies/backtest/", "Python файлы (*.py)"
        )
        if path:
            self._load_strategy_from_path(path)

    def _autoload_strategy(self, strategy_id: str | None, file_path: str | None):
        """
        Ищет on_bar в файле самого агента (strategies/bochka_cny.py),
        а не в отдельной папке backtest/.
        """
        candidates = []

        # 1. Прямо файл агента — если в нём есть on_bar, используем его
        if file_path and Path(file_path).exists():
            candidates.append(Path(file_path))

        # 2. По strategy_id в папке strategies/
        if strategy_id:
            candidates.append(STRATEGIES_DIR / f"{strategy_id}.py")
            candidates.append(STRATEGIES_DIR / f"{strategy_id.lower()}.py")

        for path in candidates:
            if path.exists():
                import importlib.util
                spec = importlib.util.spec_from_file_location("_bt_check", str(path))
                module = importlib.util.module_from_spec(spec)
                try:
                    spec.loader.exec_module(module)
                except Exception:
                    continue

                if hasattr(module, "on_bar"):
                    logger.info(f"Автозагрузка бэктест-стратегии: {path}")
                    self._load_strategy_from_path(str(path))
                    return

        logger.debug(
            f"on_bar не найден ни в одном из кандидатов: "
            f"{[str(c) for c in candidates]}"
        )
        self._strategy_label.setText(
            "⚠  Выбери файл стратегии вручную ↓"
        )
        self._strategy_label.setStyleSheet(
            "color: #f9e2af; font-size: 11px; padding: 6px 8px; "
            "background: #181825; border: 1px solid #f9e2af; border-radius: 4px;"
        )

    def _load_strategy_from_path(self, path: str):
        """Загружает модуль стратегии, проверяет наличие on_bar."""
        import importlib.util

        try:
            spec = importlib.util.spec_from_file_location("_bt_strategy", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            QMessageBox.critical(self, "Ошибка загрузки", str(e))
            logger.error(f"Не удалось загрузить стратегию: {e}")
            return

        if not hasattr(module, "on_bar"):
            QMessageBox.warning(
                self, "Нет функции on_bar",
                f"В файле нет функции on_bar(bars, position, params):\n{path}"
            )
            return

        self._strategy_module = module
        name = module.get_info()["name"] if hasattr(module, "get_info") else Path(path).stem
        self._strategy_label.setText(f"{name}  ←  {Path(path).name}")
        self._strategy_label.setToolTip(path)
        logger.info(f"Стратегия загружена: {name}")

        # Строим виджеты параметров из get_params()
        if hasattr(module, "get_params"):
            self._build_params_widgets(module.get_params())
        else:
            self._params_box.setVisible(False)

        self._update_run_button()

    # ── Логика ───────────────────────────────────────────────────────────────

    def _choose_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Выбрать файл котировок",
            str(Path.home()),
            "Text files (*.txt *.csv);;All files (*)"
        )
        if not path:
            return
        self._data_filepath = path
        self._file_label.setText(Path(path).name)
        self._file_label.setToolTip(path)
        self._update_run_button()

    def _update_run_button(self):
        ready = bool(self._data_filepath and hasattr(self, "_strategy_module"))
        self._btn_run.setEnabled(ready)

    def _build_strategy(self):
        """
        Возвращает модуль стратегии с обновлёнными параметрами через monkey-patch.
        Класс: BacktestWindow
        """
        # Применяем значения из виджетов поверх дефолтов
        if hasattr(self._strategy_module, "get_params"):
            original = self._strategy_module.get_params()
            for fname, widget in self._param_widgets.items():
                if fname not in original:
                    continue
                if isinstance(widget, QComboBox):
                    original[fname]["default"] = widget.currentData()
                else:
                    original[fname]["default"] = widget.value()
            self._strategy_module.get_params = lambda: original

        return self._strategy_module

    def _run_backtest(self):
        if not self._data_filepath or not hasattr(self, "_strategy_module"):
            return

        self._btn_run.setEnabled(False)
        self._progress.setVisible(True)

        module = self._build_strategy()
        # Получаем board из параметров стратегии
        board = self._board
        # Пытаемся получить board из виджета параметров если есть
        if "board" in self._param_widgets and isinstance(self._param_widgets["board"], QComboBox):
            board = self._param_widgets["board"].currentData() or board
        
        self._worker = BacktestWorker(
            module, self._data_filepath, 
            connector_id=self._connector_id,
            board=board
        )
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_finished(self, result: BacktestResult):
        self._progress.setVisible(False)
        self._btn_run.setEnabled(True)
        logger.info("Бэктест завершён, открываем отчёт")
        from ui.backtest_report import BacktestReport
        BacktestReport(result, parent=self).exec()

    def _on_error(self, message: str):
        self._progress.setVisible(False)
        self._btn_run.setEnabled(True)
        QMessageBox.critical(self, "Ошибка бэктеста", message)

    def _stop_worker(self):
        if self._worker and self._worker.isRunning():
            logger.info("Остановка бэктеста...")
            self._worker.stop()
            self._worker.wait(3000)
            if self._worker.isRunning():
                self._worker.terminate()
            self._worker = None
            self._progress.setVisible(False)
            self._btn_run.setEnabled(True)
            logger.info("Бэктест остановлен")

    def closeEvent(self, event):
        self._stop_worker()
        event.accept()

    def reject(self):
        self._stop_worker()
        super().reject()
