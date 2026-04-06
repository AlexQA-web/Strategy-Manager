"""Regression-тесты для core/autostart.py."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core import autostart


class TestStartLiveEngineContract:
    """Проверки реестра engine при failed start."""

    def setup_method(self):
        with autostart._engine_state_lock:
            autostart._live_engines.clear()
            autostart._launching_engines.clear()
            autostart._runtime_states.clear()
            autostart._strategy_ownership.clear()

    def teardown_method(self):
        with autostart._engine_state_lock:
            autostart._live_engines.clear()
            autostart._launching_engines.clear()
            autostart._runtime_states.clear()
            autostart._strategy_ownership.clear()

    def test_failed_engine_start_is_not_registered(self):
        connector = MagicMock()
        connector.is_connected.return_value = True

        loaded = MagicMock()
        loaded.params_schema = {}
        loaded.call_on_start.return_value = True
        loaded.module = SimpleNamespace(on_bar=MagicMock())

        engine = MagicMock()
        engine.start.return_value = False

        strategy_data = {
            "file_path": "strategies/example_strategy.py",
            "connector_id": "finam",
            "account_id": "acc-1",
            "ticker": "SBER",
            "board": "TQBR",
            "timeframe": "5m",
            "params": {},
        }

        with patch("core.storage.get_strategy", return_value=strategy_data), \
             patch("core.strategy_loader.strategy_loader") as mock_loader, \
             patch("core.connector_manager.connector_manager") as mock_connector_manager, \
             patch("core.scheduler.is_in_schedule", return_value=True), \
             patch("core.live_engine.LiveEngine", return_value=engine):
            mock_loader.get.return_value = loaded
            mock_connector_manager.get.return_value = connector

            started = autostart.start_live_engine("strategy-1")

        assert started is False
        assert autostart.get_live_engines() == {}

    def test_preflight_runs_before_on_start_and_engine_start(self):
        connector = MagicMock()
        connector.is_connected.return_value = True

        loaded = MagicMock()
        loaded.params_schema = {}
        loaded.module = SimpleNamespace(on_bar=MagicMock())

        call_order = []

        def _on_start(params, conn):
            call_order.append("on_start")
            return True

        loaded.call_on_start.side_effect = _on_start

        engine = MagicMock()
        engine.runtime_state = "synced"
        engine.sync_status = "synced"
        engine.startup_preflight.side_effect = lambda: call_order.append("preflight") or True
        engine.start.side_effect = lambda: call_order.append("start") or True
        engine.is_running = True

        strategy_data = {
            "file_path": "strategies/example_strategy.py",
            "connector_id": "finam",
            "account_id": "acc-1",
            "ticker": "SBER",
            "board": "TQBR",
            "timeframe": "5m",
            "params": {},
        }

        with patch("core.storage.get_strategy", return_value=strategy_data), \
             patch("core.strategy_loader.strategy_loader") as mock_loader, \
             patch("core.connector_manager.connector_manager") as mock_connector_manager, \
             patch("core.scheduler.is_in_schedule", return_value=True), \
             patch("core.live_engine.LiveEngine", return_value=engine):
            mock_loader.get.return_value = loaded
            mock_connector_manager.get.return_value = connector

            started = autostart.start_live_engine("strategy-1")

        assert started is True
        assert call_order == ["preflight", "on_start", "start"]

    def test_failed_preflight_sets_failed_start_runtime_state(self):
        connector = MagicMock()
        connector.is_connected.return_value = True

        loaded = MagicMock()
        loaded.params_schema = {}
        loaded.module = SimpleNamespace(on_bar=MagicMock())
        loaded.call_on_start.return_value = True

        engine = MagicMock()
        engine.startup_preflight.return_value = False
        engine.runtime_state = "failed_start"
        engine.sync_status = "stale"

        strategy_data = {
            "file_path": "strategies/example_strategy.py",
            "connector_id": "finam",
            "account_id": "acc-1",
            "ticker": "SBER",
            "board": "TQBR",
            "timeframe": "5m",
            "params": {},
        }

        with patch("core.storage.get_strategy", return_value=strategy_data), \
             patch("core.strategy_loader.strategy_loader") as mock_loader, \
             patch("core.connector_manager.connector_manager") as mock_connector_manager, \
             patch("core.scheduler.is_in_schedule", return_value=True), \
             patch("core.live_engine.LiveEngine", return_value=engine):
            mock_loader.get.return_value = loaded
            mock_connector_manager.get.return_value = connector

            started = autostart.start_live_engine("strategy-1")

        runtime = autostart.get_strategy_runtime_status("strategy-1")
        assert started is False
        assert runtime["actual_state"] == "failed_start"
        loaded.call_on_start.assert_not_called()

    def test_manual_intervention_preflight_preserves_runtime_state(self):
        connector = MagicMock()
        connector.is_connected.return_value = True

        loaded = MagicMock()
        loaded.params_schema = {}
        loaded.module = SimpleNamespace(on_bar=MagicMock())
        loaded.call_on_start.return_value = True

        engine = MagicMock()
        engine.startup_preflight.return_value = False
        engine.runtime_state = "manual_intervention_required"
        engine.sync_status = "stale"

        strategy_data = {
            "file_path": "strategies/example_strategy.py",
            "connector_id": "finam",
            "account_id": "acc-1",
            "ticker": "SBER",
            "board": "TQBR",
            "timeframe": "5m",
            "params": {},
        }

        with patch("core.storage.get_strategy", return_value=strategy_data), \
             patch("core.strategy_loader.strategy_loader") as mock_loader, \
             patch("core.connector_manager.connector_manager") as mock_connector_manager, \
             patch("core.scheduler.is_in_schedule", return_value=True), \
             patch("core.live_engine.LiveEngine", return_value=engine):
            mock_loader.get.return_value = loaded
            mock_connector_manager.get.return_value = connector

            started = autostart.start_live_engine("strategy-1")

        runtime = autostart.get_strategy_runtime_status("strategy-1")
        assert started is False
        assert runtime["actual_state"] == "manual_intervention_required"
        loaded.call_on_start.assert_not_called()

    def test_watchdog_disconnect_stops_runtime_without_changing_desired_state(self):
        engine = MagicMock()
        engine.sync_status = "synced"
        engine.stop.return_value = None

        strategy_data = {
            "connector_id": "finam",
            "desired_state": "active",
            "status": "active",
            "is_enabled": True,
            "params": {},
        }

        connector = MagicMock()
        connector.is_connected.return_value = False

        with autostart._engine_state_lock:
            autostart._live_engines["strategy-1"] = engine
            autostart._runtime_states["strategy-1"] = {
                "actual_state": "trading",
                "sync_status": "synced",
                "is_running": True,
            }
        autostart._connector_states = {"finam": True}

        with patch("core.storage.get_bool_setting", return_value=True), \
             patch("core.storage.get_all_strategies", return_value={"strategy-1": strategy_data}), \
             patch("core.storage.get_strategy", return_value=strategy_data), \
             patch("core.connector_manager.connector_manager") as mock_connector_manager, \
             patch("core.strategy_loader.strategy_loader") as mock_loader:
            mock_connector_manager.all.return_value = {"finam": connector}
            mock_connector_manager.get.return_value = connector
            mock_loader.get.return_value = None

            autostart._sync_engines_with_connectors()

        runtime = autostart.get_strategy_runtime_status("strategy-1")
        assert strategy_data["desired_state"] == "active"
        assert strategy_data["status"] == "active"
        assert runtime["actual_state"] == "stopped"
        engine.stop.assert_called_once()

    def test_watchdog_reconnect_starts_desired_active_strategy(self):
        strategy_data = {
            "connector_id": "finam",
            "desired_state": "active",
            "status": "active",
            "is_enabled": True,
            "file_path": "strategies/example_strategy.py",
        }
        connector = MagicMock()
        connector.is_connected.return_value = True

        autostart._connector_states = {"finam": False}

        with patch("core.storage.get_bool_setting", return_value=True), \
             patch("core.storage.get_all_strategies", return_value={"strategy-1": strategy_data}), \
             patch("core.connector_manager.connector_manager") as mock_connector_manager, \
             patch("core.scheduler.is_in_schedule", return_value=True), \
             patch("core.autostart.start_live_engine", return_value=True) as mock_start:
            mock_connector_manager.all.return_value = {"finam": connector}

            autostart._sync_engines_with_connectors()

        mock_start.assert_called_once_with("strategy-1", wait_for_connection=False)

    def test_watchdog_reconnect_starts_all_desired_active_strategies(self):
        strategies = {
            "strategy-1": {
                "connector_id": "finam",
                "desired_state": "active",
                "status": "active",
                "is_enabled": True,
                "file_path": "strategies/example_strategy.py",
            },
            "strategy-2": {
                "connector_id": "finam",
                "desired_state": "active",
                "status": "active",
                "is_enabled": True,
                "file_path": "strategies/example_strategy.py",
            },
        }
        connector = MagicMock()
        connector.is_connected.return_value = True

        autostart._connector_states = {"finam": False}

        with patch("core.storage.get_bool_setting", return_value=True), \
             patch("core.storage.get_all_strategies", return_value=strategies), \
             patch("core.connector_manager.connector_manager") as mock_connector_manager, \
             patch("core.scheduler.is_in_schedule", return_value=True), \
             patch("core.autostart.start_live_engine", return_value=True) as mock_start:
            mock_connector_manager.all.return_value = {"finam": connector}

            autostart._sync_engines_with_connectors()

        assert mock_start.call_count == 2
        mock_start.assert_any_call("strategy-1", wait_for_connection=False)
        mock_start.assert_any_call("strategy-2", wait_for_connection=False)

    def test_ownership_conflict_blocks_second_strategy_start(self):
        connector = MagicMock()
        connector.is_connected.return_value = True

        loaded = MagicMock()
        loaded.params_schema = {}
        loaded.module = SimpleNamespace(on_bar=MagicMock())
        loaded.call_on_start.return_value = True

        with autostart._engine_state_lock:
            autostart._strategy_ownership[("acc-1", "SBER")] = "other-strategy"

        strategy_data = {
            "file_path": "strategies/example_strategy.py",
            "connector_id": "finam",
            "account_id": "acc-1",
            "ticker": "SBER",
            "board": "TQBR",
            "timeframe": "5m",
            "params": {},
        }

        with patch("core.storage.get_strategy", return_value=strategy_data), \
             patch("core.strategy_loader.strategy_loader") as mock_loader, \
             patch("core.connector_manager.connector_manager") as mock_connector_manager, \
             patch("core.scheduler.is_in_schedule", return_value=True):
            mock_loader.get.return_value = loaded
            mock_connector_manager.get.return_value = connector

            started = autostart.start_live_engine("strategy-1")

        runtime = autostart.get_strategy_runtime_status("strategy-1")
        assert started is False
        assert runtime["actual_state"] == "manual_intervention_required"
        loaded.call_on_start.assert_not_called()
