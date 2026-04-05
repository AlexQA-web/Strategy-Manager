# tests/test_chart_cache.py

"""Тесты для core/chart_cache.py — per-key lock и cleanup (TASK-010)."""

import threading
from unittest.mock import patch
import pandas as pd
import pytest

from core.chart_cache import save, load, cleanup_tmp_files, _get_key_lock


def _make_df(n=5):
    """Создаёт тестовый DataFrame с OHLCV."""
    idx = pd.date_range("2026-01-01", periods=n, freq="5min")
    return pd.DataFrame(
        {"Open": range(n), "High": range(n), "Low": range(n),
         "Close": range(n), "Volume": range(n)},
        index=idx,
    )


class TestPerKeyLock:
    """Тесты per-key lock для chart_cache."""

    def test_same_key_returns_same_lock(self):
        """Один ключ — один lock."""
        lock1 = _get_key_lock("TQBR/SBER/5m")
        lock2 = _get_key_lock("TQBR/SBER/5m")
        assert lock1 is lock2

    def test_different_keys_return_different_locks(self):
        """Разные ключи — разные locks."""
        lock1 = _get_key_lock("TQBR/SBER/5m")
        lock2 = _get_key_lock("FUT/SiM5/1m")
        assert lock1 is not lock2

    def test_concurrent_save_load(self, tmp_path):
        """Конкурентные save/load не ломают данные."""
        df = _make_df(10)
        errors = []

        with patch("core.chart_cache.CACHE_DIR", tmp_path):
            def writer():
                for _ in range(5):
                    try:
                        save("TEST", "5m", df, board="TQBR")
                    except Exception as e:
                        errors.append(e)

            def reader():
                for _ in range(5):
                    try:
                        result = load("TEST", "5m", board="TQBR")
                        if result is not None:
                            assert len(result) == 10
                    except Exception as e:
                        errors.append(e)

            threads = [threading.Thread(target=writer) for _ in range(3)]
            threads += [threading.Thread(target=reader) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        assert not errors, f"Errors during concurrent access: {errors}"


class TestCleanupTmpFiles:
    """Тесты cleanup_tmp_files."""

    def test_removes_orphan_tmp(self, tmp_path):
        """Удаляет orphan .tmp файлы."""
        # Создаём структуру
        subdir = tmp_path / "TQBR" / "SBER"
        subdir.mkdir(parents=True)
        orphan = subdir / "5m.pkl.tmp"
        orphan.write_bytes(b"garbage")
        real = subdir / "5m.pkl"
        real.write_bytes(b"real data")

        with patch("core.chart_cache.CACHE_DIR", tmp_path):
            cleanup_tmp_files()

        assert not orphan.exists()
        assert real.exists()

    def test_no_error_on_missing_cache_dir(self, tmp_path):
        """Не падает если CACHE_DIR не существует."""
        with patch("core.chart_cache.CACHE_DIR", tmp_path / "nonexistent"):
            cleanup_tmp_files()  # не должно бросить


class TestSaveLoad:
    """Тесты save/load с per-key lock."""

    def test_save_and_load_roundtrip(self, tmp_path):
        """save → load возвращает те же данные."""
        df = _make_df(5)
        with patch("core.chart_cache.CACHE_DIR", tmp_path):
            save("SBER", "5m", df)
            result = load("SBER", "5m")

        assert result is not None
        assert len(result) == 5

    def test_save_cleans_tmp_on_error(self, tmp_path):
        """При ошибке .tmp удаляется."""
        with patch("core.chart_cache.CACHE_DIR", tmp_path):
            # Передаём невалидные данные
            bad_df = pd.DataFrame({"Open": [1]}, index=[0])

            with patch("core.chart_cache.pickle.dump", side_effect=RuntimeError("fail")):
                save("SBER", "5m", bad_df)

            # .tmp не должен остаться
            tmps = list(tmp_path.rglob("*.tmp"))
            assert len(tmps) == 0
