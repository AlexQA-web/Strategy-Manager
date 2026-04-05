# core/rwlock.py

"""Readers-Writer Lock (RWLock).

Позволяет множественным потокам читать одновременно,
но запись требует эксклюзивного доступа.

Приоритет: писатели (write-preferring) — если писатель ждёт,
новые читатели блокируются, чтобы избежать starvation писателей.
"""

import threading
from contextlib import contextmanager
from typing import Generator


class RWLock:
    """Readers-Writer Lock с приоритетом писателей.

    Usage:
        rwlock = RWLock()

        # Чтение (множественные потоки могут читать одновременно)
        with rwlock.read_lock():
            data = read_something()

        # Запись (эксклюзивный доступ)
        with rwlock.write_lock():
            write_something()
    """

    def __init__(self) -> None:
        self._readers = 0
        self._writers = 0
        self._writers_waiting = 0
        self._lock = threading.Lock()
        self._read_ready = threading.Condition(self._lock)
        self._write_ready = threading.Condition(self._lock)

    @contextmanager
    def read_lock(self) -> Generator[None, None, None]:
        """Контекстный менеджер для чтения.

        Множественные читатели могут работать одновременно,
        но если писатель ждёт или пишет — читатели блокируются.
        """
        with self._lock:
            # Ждём пока нет активных писателей и ожидающих писателей
            while self._writers > 0 or self._writers_waiting > 0:
                self._read_ready.wait()
            self._readers += 1

        try:
            yield
        finally:
            with self._lock:
                self._readers -= 1
                if self._readers == 0:
                    # Последний читатель уведомляет ожидающих писателей
                    self._write_ready.notify_all()

    @contextmanager
    def write_lock(self) -> Generator[None, None, None]:
        """Контекстный менеджер для записи.

        Эксклюзивный доступ — ни один другой читатель или писатель
        не может работать одновременно.
        """
        with self._lock:
            self._writers_waiting += 1
            # Ждём пока нет активных читателей или писателей
            while self._readers > 0 or self._writers > 0:
                self._write_ready.wait()
            self._writers_waiting -= 1
            self._writers += 1

        try:
            yield
        finally:
            with self._lock:
                self._writers -= 1
                # Уведомляем всех ожидающих (и читателей, и писателей)
                self._write_ready.notify_all()
                self._read_ready.notify_all()

    @contextmanager
    def read_ctx(self) -> Generator[None, None, None]:
        """Алиас для read_lock()."""
        with self.read_lock():
            yield

    @contextmanager
    def write_ctx(self) -> Generator[None, None, None]:
        """Алиас для write_lock()."""
        with self.write_lock():
            yield
