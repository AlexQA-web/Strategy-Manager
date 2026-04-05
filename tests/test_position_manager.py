"""Тесты PositionManager: subscribe/unsubscribe (TASK-017)."""

from unittest.mock import MagicMock, patch

import pytest

from core.position_manager import PositionManager


class TestPositionManagerSubscription:

    def _make_connector(self):
        conn = MagicMock()
        conn.is_connected.return_value = True
        conn.get_all_positions.return_value = {}
        return conn

    @patch("core.position_manager.PositionManager._connector")
    def test_bind_uses_subscribe_not_singleton(self, mock_conn_method):
        """bind() должен использовать subscribe_positions, а не on_positions_update."""
        conn = self._make_connector()
        mock_conn_method.return_value = conn

        pm = PositionManager()
        pm.bind("finam")

        conn.subscribe_positions.assert_called_once()
        conn.on_positions_update.assert_not_called()

    @patch("core.position_manager.PositionManager._connector")
    def test_unbind_removes_only_own_listener(self, mock_conn_method):
        """При переключении коннектора отписывается только собственный callback."""
        old_conn = self._make_connector()
        new_conn = self._make_connector()
        mock_conn_method.return_value = old_conn

        pm = PositionManager()
        pm.bind("finam")

        # Переключаемся — должен отписаться от старого
        mock_conn_method.return_value = new_conn
        pm.bind("quik")

        old_conn.unsubscribe_positions.assert_called_once_with(pm._on_positions_update)
        old_conn.off_positions_update.assert_not_called()

    @patch("core.position_manager.PositionManager._connector")
    def test_ui_listener_survives_bind(self, mock_conn_method):
        """UI подписчики через subscribe_positions не затрагиваются при bind."""
        from core.base_connector import BaseConnector

        # Создаём реальный mock с настоящими списками listener-ов
        conn = self._make_connector()
        listeners = []

        def subscribe(cb):
            listeners.append(cb)

        def unsubscribe(cb):
            listeners[:] = [x for x in listeners if x is not cb]

        conn.subscribe_positions.side_effect = subscribe
        conn.unsubscribe_positions.side_effect = unsubscribe
        mock_conn_method.return_value = conn

        # UI подписывается
        ui_callback = MagicMock()
        listeners.append(ui_callback)

        # PositionManager подписывается
        pm = PositionManager()
        pm.bind("finam")

        assert ui_callback in listeners, "UI callback должен остаться"
        assert pm._on_positions_update in listeners

        # Переключаем коннектор — UI callback должен остаться
        pm.bind("finam")
        assert ui_callback in listeners, "UI callback не должен быть удалён при rebind"
