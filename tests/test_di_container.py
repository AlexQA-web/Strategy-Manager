"""Тесты для core/di_container.py — включая concurrent resolve."""

import threading
from unittest.mock import MagicMock

from core.di_container import DIContainer


class TestDIContainerBasic:
    """Базовые тесты DI-контейнера."""

    def test_register_and_resolve_instance(self):
        c = DIContainer()
        obj = object()
        c.register(object, obj)
        assert c.resolve(object) is obj

    def test_register_and_resolve_factory(self):
        c = DIContainer()
        c.register(list, factory=lambda: [1, 2, 3])
        result = c.resolve(list)
        assert result == [1, 2, 3]

    def test_singleton_returns_same_instance(self):
        c = DIContainer()
        c.register(list, factory=lambda: [], singleton=True)
        a = c.resolve(list)
        b = c.resolve(list)
        assert a is b

    def test_non_singleton_returns_new_instance(self):
        c = DIContainer()
        c.register(list, factory=lambda: [], singleton=False)
        a = c.resolve(list)
        b = c.resolve(list)
        assert a is not b

    def test_resolve_unregistered_raises_key_error(self):
        c = DIContainer()
        try:
            c.resolve(dict)
            assert False, "Should have raised KeyError"
        except KeyError:
            pass

    def test_resolve_optional_returns_none(self):
        c = DIContainer()
        assert c.resolve_optional(dict) is None

    def test_has(self):
        c = DIContainer()
        assert c.has(list) is False
        c.register(list, factory=lambda: [])
        assert c.has(list) is True

    def test_clear(self):
        c = DIContainer()
        c.register(list, factory=lambda: [])
        c.clear()
        assert c.has(list) is False

    def test_register_class(self):
        c = DIContainer()
        c.register(list, list)
        result = c.resolve(list)
        assert isinstance(result, list)


class TestDIContainerConcurrency:
    """Тесты потокобезопасности resolve()."""

    def test_concurrent_resolve_singleton_returns_same(self):
        """Множественные потоки resolve() для singleton должны получить один объект."""
        c = DIContainer()
        call_count = 0

        def slow_factory():
            nonlocal call_count
            call_count += 1
            return {"id": call_count}

        c.register(dict, factory=slow_factory, singleton=True)

        results = []
        errors = []
        barrier = threading.Barrier(10)

        def worker():
            try:
                barrier.wait(timeout=5)
                obj = c.resolve(dict)
                results.append(id(obj))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"Errors: {errors}"
        # Все потоки должны получить один и тот же объект
        assert len(set(results)) == 1

    def test_concurrent_register_and_resolve(self):
        """Параллельные register и resolve не вызывают crash."""
        c = DIContainer()
        errors = []

        def registerer():
            for i in range(100):
                try:
                    c.register(type(f"Type{i}", (), {}), factory=lambda: object(), singleton=False)
                except Exception as e:
                    errors.append(e)

        def resolver():
            for _ in range(100):
                try:
                    c.resolve_optional(list)
                except Exception as e:
                    errors.append(e)

        t1 = threading.Thread(target=registerer)
        t2 = threading.Thread(target=resolver)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert not errors
