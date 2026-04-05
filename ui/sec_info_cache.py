"""Кэш sec_info с TTL и фоновым обновлением.

Извлечён из MainWindow для уменьшения god-class.
"""

import threading
import time


class SecInfoCache:
    """Неблокирующий кэш для sec_info с TTL и eviction."""

    def __init__(self, ttl: float = 60.0, max_age: float = 300.0):
        self._cache: dict = {}  # {(connector_id, ticker, board): (sec_info, timestamp)}
        self._refreshing: set = set()
        self._ttl = ttl
        self._max_age = max_age

    def get(self, connector, connector_id: str, ticker: str, board: str):
        """Возвращает sec_info из кэша или None, не блокируя вызывающий поток."""
        key = (connector_id, ticker, board)
        cached = self._cache.get(key)
        now = time.monotonic()

        if cached:
            sec_info, ts = cached
            if now - ts < self._ttl:
                return sec_info

        # Кэш устарел — обновляем в фоне, возвращаем старое значение
        self._refresh_background(connector, key)
        return cached[0] if cached else None

    def evict_stale(self):
        """Удаляет записи старше max_age. Вызывать периодически."""
        now = time.monotonic()
        stale = [k for k, (_, ts) in self._cache.items()
                 if now - ts > self._max_age]
        for k in stale:
            del self._cache[k]

    def _refresh_background(self, connector, key: tuple):
        """Запускает фоновый поток для обновления sec_info."""
        if key in self._refreshing:
            return
        self._refreshing.add(key)

        _, ticker, board = key

        def _fetch():
            try:
                sec_info = connector.get_sec_info(ticker, board)
                if sec_info:
                    self._cache[key] = (sec_info, time.monotonic())
            except Exception:
                pass
            finally:
                self._refreshing.discard(key)

        threading.Thread(target=_fetch, daemon=True).start()
