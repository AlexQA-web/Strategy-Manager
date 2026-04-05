"""Общие фикстуры для всех тестов."""

import sys
import pytest
from loguru import logger


@pytest.fixture(autouse=True)
def _suppress_loguru(monkeypatch):
    """Подавляет loguru вывод в stderr во время тестов.

    Предотвращает 'I/O operation on closed file' ошибки,
    когда daemon-потоки продолжают логировать после завершения теста.
    """
    logger.remove()
    logger.add(sys.stderr, level="CRITICAL")  # только CRITICAL во время тестов
    yield
    logger.remove()


@pytest.fixture(autouse=True)
def _reset_fill_ledger():
    """Сбрасывает in-memory state FillLedger между тестами."""
    from core.fill_ledger import fill_ledger
    fill_ledger._seen_fills.clear()
    yield
    fill_ledger._seen_fills.clear()
