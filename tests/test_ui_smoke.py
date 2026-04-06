import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton, QWidget

from ui.positions_panel import PartialCloseDialog, PositionsPanel
from ui.strategy_window import StrategyWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_partial_close_dialog_smoke(qapp):
    dialog = PartialCloseDialog("SBER", 7)

    assert dialog.quantity() == 7
    assert dialog.windowTitle()


def test_positions_panel_close_all_requires_confirmation(qapp, monkeypatch):
    monkeypatch.setattr("ui.positions_panel.position_manager.on_update", lambda callback: None)
    monkeypatch.setattr("ui.positions_panel.position_manager.get_positions", lambda account_id: [])
    monkeypatch.setattr("ui.positions_panel.position_manager.close_all_positions", lambda account_id: 2)

    panel = PositionsPanel(account_id="ACC1")
    panel._table.setRowCount(1)
    question_mock = MagicMock(return_value=QMessageBox.StandardButton.No)
    monkeypatch.setattr("ui.positions_panel.QMessageBox.question", question_mock)

    panel._on_close_all_account()

    question_mock.assert_called_once()


def test_positions_panel_partial_close_uses_fresh_qty(qapp, monkeypatch):
    monkeypatch.setattr("ui.positions_panel.position_manager.on_update", lambda callback: None)
    monkeypatch.setattr("ui.positions_panel.position_manager.get_positions", lambda account_id: [])
    panel = PositionsPanel(account_id="ACC1")
    captured = {}

    class FakeDialog:
        def __init__(self, ticker, max_qty, parent=None):
            captured["ticker"] = ticker
            captured["max_qty"] = max_qty

        def exec(self):
            return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(panel, "_resolve_current_position_qty", lambda ticker: 9)
    monkeypatch.setattr("ui.positions_panel.PartialCloseDialog", FakeDialog)

    panel._on_partial_close("SBER")

    assert captured == {"ticker": "SBER", "max_qty": 9}


def test_strategy_window_stop_requires_confirmation(qapp, monkeypatch):
    monkeypatch.setattr("ui.strategy_window.get_strategy", lambda sid: {"name": sid, "params": {}})
    monkeypatch.setattr("ui.strategy_window.strategy_loader.get", lambda sid: SimpleNamespace(params_schema={}))
    monkeypatch.setattr(StrategyWindow, "_build_header", lambda self: QWidget())
    monkeypatch.setattr(StrategyWindow, "tab_overview", lambda self: QWidget())
    monkeypatch.setattr(StrategyWindow, "tab_params", lambda self: QWidget())
    monkeypatch.setattr(StrategyWindow, "_tab_lot_sizing", lambda self: QWidget())
    monkeypatch.setattr(StrategyWindow, "_tab_positions", lambda self: QWidget())
    monkeypatch.setattr(StrategyWindow, "_tab_order", lambda self: QWidget())
    monkeypatch.setattr(StrategyWindow, "_sync_tickers", lambda self: None)
    monkeypatch.setattr(StrategyWindow, "_start_runtime_timer", lambda self: None)
    monkeypatch.setattr(StrategyWindow, "_refresh_runtime_status", lambda self: None)

    window = StrategyWindow("sid-1")
    window.btn_stop = QPushButton()
    window.lbl_status = QLabel()
    question_mock = MagicMock(return_value=QMessageBox.StandardButton.No)
    stop_mock = MagicMock()
    monkeypatch.setattr("ui.strategy_window.QMessageBox.question", question_mock)
    monkeypatch.setattr("core.autostart.stop_live_engine", stop_mock)

    window._stop_strategy()

    question_mock.assert_called_once()
    stop_mock.assert_not_called()