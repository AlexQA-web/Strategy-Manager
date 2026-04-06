"""Тесты HealthServer: secure-by-default (TASK-018)."""

from unittest.mock import patch, MagicMock

import pytest

from core.health_server import HealthServer, HealthCheckHandler, configure_default_callbacks


class TestHealthServerSecure:

    def test_disabled_by_default(self):
        hs = HealthServer(port=9999, enabled=False)
        hs.start()
        assert not hs.is_running

    def test_loopback_enforced(self):
        hs = HealthServer(host='0.0.0.0', port=9999, enabled=True)
        assert hs._host == '127.0.0.1'

    def test_localhost_allowed(self):
        hs = HealthServer(host='localhost', port=9999, enabled=True)
        assert hs._host == 'localhost'

    def test_start_when_enabled(self):
        hs = HealthServer(port=0, enabled=True)
        try:
            hs.start()
            assert hs.is_running
        finally:
            hs.stop()

    def test_double_start_ignored(self):
        hs = HealthServer(port=0, enabled=True)
        try:
            hs.start()
            hs.start()  # second call is no-op
            assert hs.is_running
        finally:
            hs.stop()

    def test_stop_idempotent(self):
        hs = HealthServer(port=9999, enabled=False)
        hs.stop()  # should not raise
        hs.stop()

    def test_auth_token_set_on_handler(self):
        hs = HealthServer(port=0, enabled=True, auth_token='secret123')
        try:
            hs.start()
            assert HealthCheckHandler.auth_token == 'secret123'
        finally:
            hs.stop()


class TestHealthCheckHandlerAuth:

    def test_check_auth_valid(self):
        handler = MagicMock(spec=HealthCheckHandler)
        handler.auth_token = 'mytoken'
        handler.headers = {'Authorization': 'Bearer mytoken'}
        assert HealthCheckHandler._check_auth(handler) is True

    def test_check_auth_invalid(self):
        handler = MagicMock(spec=HealthCheckHandler)
        handler.auth_token = 'mytoken'
        handler.headers = {'Authorization': 'Bearer wrong'}
        assert HealthCheckHandler._check_auth(handler) is False

    def test_check_auth_missing_header(self):
        handler = MagicMock(spec=HealthCheckHandler)
        handler.auth_token = 'mytoken'
        handler.headers = {}
        assert HealthCheckHandler._check_auth(handler) is False


class TestHealthServerObservability:

    def test_configure_default_callbacks_wires_observability(self, monkeypatch):
        hs = HealthServer(port=9999, enabled=False)

        monkeypatch.setattr(
            "core.observability.collect_health_snapshot",
            lambda: {"status": "ok"},
        )
        monkeypatch.setattr(
            "core.observability.collect_runtime_metrics",
            lambda: {"latency": {}},
        )
        monkeypatch.setattr(
            "core.observability.collect_strategies_health",
            lambda: {"sid": {"actual_state": "trading"}},
        )

        configured = configure_default_callbacks(hs)

        assert configured is hs
        assert HealthCheckHandler.health_callback() == {"status": "ok"}
        assert HealthCheckHandler.metrics_callback() == {"latency": {}}
        assert HealthCheckHandler.strategies_callback() == {"sid": {"actual_state": "trading"}}
