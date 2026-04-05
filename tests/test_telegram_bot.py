"""Тесты TelegramNotifier lifecycle (TASK-019)."""

import asyncio
import threading
from unittest.mock import patch, MagicMock

import pytest


class TestTelegramNotifierLifecycle:

    def _make_notifier(self):
        with patch.dict('sys.modules', {
            'telegram': MagicMock(),
            'telegram.error': MagicMock(),
        }):
            # Сбрасываем глобальный синглтон — не трогаем, создаём свой экземпляр
            from core.telegram_bot import TelegramNotifier
            return TelegramNotifier()

    def test_lazy_start_no_thread_on_init(self):
        """При создании TelegramNotifier event loop НЕ запускается сразу."""
        n = self._make_notifier()
        assert n._loop is None
        assert n._thread is None
        assert not n._started

    def test_ensure_loop_starts_thread(self):
        """_ensure_loop() запускает event loop при первом вызове."""
        n = self._make_notifier()
        n._ensure_loop()
        try:
            assert n._started
            assert n._loop is not None
            assert n._thread is not None
            assert n._thread.is_alive()
        finally:
            n.stop()

    def test_ensure_loop_idempotent(self):
        """Повторный вызов _ensure_loop() не создаёт новый поток."""
        n = self._make_notifier()
        n._ensure_loop()
        thread1 = n._thread
        n._ensure_loop()
        assert n._thread is thread1
        n.stop()

    def test_stop_idempotent(self):
        """Повторный stop() безопасен."""
        n = self._make_notifier()
        n._ensure_loop()
        n.stop()
        n.stop()  # не должно бросить исключение
        assert n._stopped

    def test_stop_without_start(self):
        """stop() без предварительного start() безопасен."""
        n = self._make_notifier()
        n.stop()
        assert n._stopped

    def test_no_restart_after_stop(self):
        """После stop() повторный _ensure_loop() не запускает поток."""
        n = self._make_notifier()
        n._ensure_loop()
        n.stop()
        n._ensure_loop()
        assert n._stopped
        # loop не должен быть перезапущен
        assert not n._started or n._stopped

    def test_stop_cleans_up_loop(self):
        """После stop() event loop закрыт."""
        n = self._make_notifier()
        n._ensure_loop()
        loop = n._loop
        n.stop()
        assert loop.is_closed()
