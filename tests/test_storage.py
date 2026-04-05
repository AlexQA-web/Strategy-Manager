# tests/test_storage.py

"""Тесты для storage.py с множественными потоками."""

import json
import threading
import time
from pathlib import Path

import pytest

from core.storage import (
    _read,
    _write_unsafe,
    _write_unsafe_inner,
    _cache,
    _rwlock,
)


class TestStorageConcurrency:
    """Тесты конкурентности storage.py."""

    def test_cache_invalidation_after_write(self, tmp_path):
        """Кэш инвалидируется после записи."""
        test_file = tmp_path / 'test_cache.json'
        test_file.write_text('{"value": 1}', encoding='utf-8')

        # Читаем — попадает в кэш
        data1 = _read(test_file)
        assert data1['value'] == 1

        # Пишем новое значение
        _write_unsafe(test_file, {'value': 2})

        # Кэш должен быть инвалидирован
        key = str(test_file)
        assert key not in _cache, "Cache should be invalidated after write"

        # Читаем снова — должно вернуть новое значение
        data2 = _read(test_file)
        assert data2['value'] == 2

    def test_multiple_readers_same_file(self, tmp_path):
        """Множественные читатели читают один файл одновременно."""
        test_file = tmp_path / 'test_multi_read.json'
        test_file.write_text('{"value": 42}', encoding='utf-8')

        results = []
        lock = threading.Lock()

        def reader():
            data = _read(test_file)
            with lock:
                results.append(data.get('value'))

        threads = [threading.Thread(target=reader) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(results) == 10
        assert all(v == 42 for v in results)

    def test_write_blocks_readers(self, tmp_path):
        """Запись блокирует читателей — они ждут завершения записи."""
        test_file = tmp_path / 'test_write_block.json'
        test_file.write_text('{"value": 1}', encoding='utf-8')

        write_started = threading.Event()
        write_done = threading.Event()
        read_during_write = []
        lock = threading.Lock()

        def writer():
            with _rwlock.write_lock():
                write_started.set()
                time.sleep(0.2)
                _write_unsafe_inner(test_file, {'value': 2})
            write_done.set()

        def reader():
            write_started.wait(timeout=5)
            data = _read(test_file)
            with lock:
                read_during_write.append(data.get('value'))

        w = threading.Thread(target=writer)
        w.start()

        write_started.wait(timeout=5)

        r = threading.Thread(target=reader)
        r.start()

        w.join(timeout=10)
        r.join(timeout=10)

        # Читатель должен был прочитать новое значение (2),
        # потому что write_lock блокирует читателей
        assert read_during_write == [2], f"Reader got stale data: {read_during_write}"

    def test_sequential_write_read(self, tmp_path):
        """Последовательные записи и чтения без гонки."""
        test_file = tmp_path / 'test_seq.json'
        test_file.write_text('{"value": 0}', encoding='utf-8')

        for i in range(1, 21):
            _write_unsafe(test_file, {'value': i})
            data = _read(test_file)
            assert data['value'] == i, f"Expected {i}, got {data['value']}"

    def test_rwlock_prevents_stale_reads(self, tmp_path):
        """RWLock гарантирует что читатели получают актуальные данные после записи."""
        test_file = tmp_path / 'test_fresh.json'
        test_file.write_text('{"value": 0}', encoding='utf-8')

        read_values = []
        lock = threading.Lock()
        write_done = threading.Event()

        def writer():
            for i in range(1, 6):
                _write_unsafe(test_file, {'value': i})
                time.sleep(0.05)
            write_done.set()

        def reader():
            for _ in range(10):
                data = _read(test_file)
                with lock:
                    read_values.append(data.get('value', 0))
                time.sleep(0.02)

        w = threading.Thread(target=writer)
        r = threading.Thread(target=reader)
        w.start()
        r.start()
        w.join(timeout=30)
        r.join(timeout=30)

        # Все прочитанные значения должны быть >= 0 и <= 5
        assert all(0 <= v <= 5 for v in read_values), f"Unexpected values: {read_values}"
        # Хотя бы одно значение должно быть > 1 (писатель продвинулся)
        assert any(v > 1 for v in read_values), f"No progress detected: {read_values}"


class TestAtomicWrite:
    """Тесты atomic-write protocol (TASK-008)."""

    def test_write_cleans_tmp_on_error(self, tmp_path):
        """При ошибке записи .tmp файл удаляется."""
        test_file = tmp_path / "test_clean.json"
        test_file.write_text('{"value": 1}', encoding="utf-8")

        # Имитируем ошибку — передаём объект, который не сериализуется
        class BadObj:
            pass

        with pytest.raises(TypeError):
            _write_unsafe(test_file, BadObj())

        tmp = test_file.with_suffix(".tmp")
        assert not tmp.exists(), "Orphan .tmp should be cleaned up"

    def test_write_creates_backup(self, tmp_path):
        """Запись создаёт бэкап предыдущей версии."""
        test_file = tmp_path / "test_bak.json"
        test_file.write_text('{"value": 1}', encoding="utf-8")

        _write_unsafe(test_file, {"value": 2})

        bak = tmp_path / "test_bak.bak.json"
        assert bak.exists(), ".bak should be created"
        with open(bak, "r", encoding="utf-8") as f:
            bak_data = json.load(f)
        assert bak_data["value"] == 1


class TestCleanupOrphanTmp:
    """Тесты cleanup_orphan_tmp (TASK-008)."""

    def test_removes_orphan_when_main_exists(self, tmp_path):
        """Удаляет orphan .tmp если основной файл в порядке."""
        from core.storage import cleanup_orphan_tmp

        main = tmp_path / "data.json"
        main.write_text('{"ok": true}', encoding="utf-8")
        orphan = tmp_path / "data.tmp"
        orphan.write_text('{"partial": true}', encoding="utf-8")

        cleanup_orphan_tmp(tmp_path)

        assert not orphan.exists()
        assert main.exists()

    def test_recovers_from_orphan_when_main_missing(self, tmp_path):
        """Восстанавливает основной файл из .tmp если основной отсутствует."""
        from core.storage import cleanup_orphan_tmp

        orphan = tmp_path / "settings.json.tmp"
        orphan.write_text('{"recovered": true}', encoding="utf-8")
        main = tmp_path / "settings.json"

        cleanup_orphan_tmp(tmp_path)

        assert not orphan.exists()
        assert main.exists()
        with open(main, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["recovered"] is True

    def test_removes_invalid_orphan(self, tmp_path):
        """Удаляет невалидный .tmp без восстановления."""
        from core.storage import cleanup_orphan_tmp

        orphan = tmp_path / "bad.json.tmp"
        orphan.write_text("not json at all {{{", encoding="utf-8")

        cleanup_orphan_tmp(tmp_path)

        assert not orphan.exists()

    def test_no_error_on_empty_dir(self, tmp_path):
        """Не падает на пустой директории."""
        from core.storage import cleanup_orphan_tmp

        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        cleanup_orphan_tmp(empty_dir)  # не должно бросить


class TestTradesTrimming:
    """Regression-тесты для trim policy trades_history (TASK-009)."""

    def test_append_trade_trims_when_over_limit(self, tmp_path, monkeypatch):
        """append_trade обрезает до _MAX_TRADES_HISTORY."""
        import core.storage as storage

        trades_file = tmp_path / "trades_history.json"
        monkeypatch.setattr(storage, "TRADES_FILE", trades_file)
        monkeypatch.setattr(storage, "_MAX_TRADES_HISTORY", 5)

        # Записываем 5 + 1 сделок
        trades_file.write_text("[]", encoding="utf-8")
        for i in range(6):
            storage.append_trade({"id": i, "ticker": "SBER"})

        result = storage._read(trades_file, use_cache=False)
        assert len(result) == 5
        # Остались последние 5 (id 1..5)
        assert result[0]["id"] == 1
        assert result[-1]["id"] == 5

    def test_append_trade_preserves_under_limit(self, tmp_path, monkeypatch):
        """append_trade не обрезает если меньше лимита."""
        import core.storage as storage

        trades_file = tmp_path / "trades_history.json"
        monkeypatch.setattr(storage, "TRADES_FILE", trades_file)
        monkeypatch.setattr(storage, "_MAX_TRADES_HISTORY", 100)

        trades_file.write_text("[]", encoding="utf-8")
        for i in range(10):
            storage.append_trade({"id": i})

        result = storage._read(trades_file, use_cache=False)
        assert len(result) == 10

    def test_trim_keeps_latest_entries(self, tmp_path, monkeypatch):
        """Trimming оставляет именно последние записи."""
        import core.storage as storage

        trades_file = tmp_path / "trades_history.json"
        monkeypatch.setattr(storage, "TRADES_FILE", trades_file)
        monkeypatch.setattr(storage, "_MAX_TRADES_HISTORY", 3)

        trades_file.write_text("[]", encoding="utf-8")
        for i in range(10):
            storage.append_trade({"seq": i})

        result = storage._read(trades_file, use_cache=False)
        assert len(result) == 3
        assert [r["seq"] for r in result] == [7, 8, 9]
