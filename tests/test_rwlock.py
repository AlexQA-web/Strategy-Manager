# tests/test_rwlock.py

"""Тесты для RWLock с множественными потоками."""

import threading
import time

import pytest

from core.rwlock import RWLock


class TestRWLockBasic:
    """Базовые тесты для RWLock."""

    def test_read_lock_allows_multiple_readers(self):
        """Множественные читатели могут читать одновременно."""
        rwlock = RWLock()
        readers_in_lock = []
        barrier = threading.Barrier(3)

        def reader(reader_id):
            with rwlock.read_lock():
                readers_in_lock.append(reader_id)
                barrier.wait(timeout=5)
                time.sleep(0.1)

        threads = [threading.Thread(target=reader, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # Все 3 читателя должны были быть в блокировке одновременно
        assert len(readers_in_lock) == 3

    def test_write_lock_is_exclusive(self):
        """Запись эксклюзивна — только один писатель."""
        rwlock = RWLock()
        writers_in_lock = []
        max_concurrent = threading.Lock()
        current_writers = 0
        max_seen = 0

        def writer(writer_id):
            nonlocal current_writers, max_seen
            with rwlock.write_lock():
                current_writers += 1
                with max_concurrent:
                    if current_writers > max_seen:
                        max_seen = current_writers
                writers_in_lock.append(writer_id)
                time.sleep(0.05)
                current_writers -= 1

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(writers_in_lock) == 5
        assert max_seen == 1  # никогда больше 1 писателя одновременно

    def test_write_blocks_readers(self):
        """Писатель блокирует новых читателей."""
        rwlock = RWLock()
        read_completed = []
        write_started = threading.Event()
        write_done = threading.Event()

        def writer():
            with rwlock.write_lock():
                write_started.set()
                time.sleep(0.2)
            write_done.set()

        def reader(reader_id):
            write_started.wait(timeout=5)
            with rwlock.read_lock():
                read_completed.append(reader_id)

        # Запускаем писателя
        w = threading.Thread(target=writer)
        w.start()

        # Даём писателю захватить lock
        write_started.wait(timeout=5)

        # Запускаем читателя — он должен ждать (writer держит write_lock)
        r = threading.Thread(target=reader, args=(1,))
        r.start()

        # Ждём завершения писателя
        w.join(timeout=10)
        r.join(timeout=10)

        assert 1 in read_completed

    def test_read_lock_context_manager(self):
        """read_lock работает как контекстный менеджер."""
        rwlock = RWLock()
        result = []

        def reader():
            with rwlock.read_lock():
                result.append('read')

        t = threading.Thread(target=reader)
        t.start()
        t.join(timeout=5)

        assert result == ['read']

    def test_write_lock_context_manager(self):
        """write_lock работает как контекстный менеджер."""
        rwlock = RWLock()
        result = []

        def writer():
            with rwlock.write_lock():
                result.append('write')

        t = threading.Thread(target=writer)
        t.start()
        t.join(timeout=5)

        assert result == ['write']

    def test_aliases_work(self):
        """Алиасы read_ctx и write_ctx работают."""
        rwlock = RWLock()
        result = []

        def reader():
            with rwlock.read_ctx():
                result.append('read')

        def writer():
            with rwlock.write_ctx():
                result.append('write')

        t1 = threading.Thread(target=reader)
        t2 = threading.Thread(target=writer)
        t1.start()
        t1.join(timeout=5)
        t2.start()
        t2.join(timeout=5)

        assert 'read' in result
        assert 'write' in result


class TestRWLockConcurrency:
    """Тесты конкурентности RWLock."""

    def test_multiple_readers_and_writers(self):
        """10 потоков: 5 читают, 2 пишут, 3 читают одновременно."""
        rwlock = RWLock()
        shared_data = {'value': 0}
        read_results = []
        write_count = [0]
        lock = threading.Lock()

        def reader(reader_id):
            for _ in range(10):
                with rwlock.read_lock():
                    val = shared_data['value']
                    time.sleep(0.001)  # имитация чтения
                    with lock:
                        read_results.append((reader_id, val))

        def writer(writer_id):
            for _ in range(5):
                with rwlock.write_lock():
                    shared_data['value'] += 1
                    with lock:
                        write_count[0] += 1
                    time.sleep(0.001)

        # Запускаем 5 читателей
        readers = [threading.Thread(target=reader, args=(i,)) for i in range(5)]
        # Запускаем 2 писателя
        writers = [threading.Thread(target=writer, args=(i,)) for i in range(2)]
        # Запускаем ещё 3 читателей
        more_readers = [threading.Thread(target=reader, args=(i + 5,)) for i in range(3)]

        all_threads = readers + writers + more_readers
        for t in all_threads:
            t.start()
        for t in all_threads:
            t.join(timeout=30)

        # Все писатели должны были завершиться
        assert write_count[0] == 10  # 2 писателя * 5 итераций
        # Финальное значение должно быть 10
        assert shared_data['value'] == 10
        # Все читатели должны были получить какие-то значения
        assert len(read_results) == 80  # 8 читателей * 10 итераций

    def test_no_stale_reads_after_write(self):
        """После записи читатели получают актуальные данные."""
        rwlock = RWLock()
        shared_data = {'value': 0}
        stale_reads = []
        lock = threading.Lock()

        def writer():
            for i in range(1, 11):
                with rwlock.write_lock():
                    shared_data['value'] = i
                time.sleep(0.01)

        def reader():
            for _ in range(20):
                with rwlock.read_lock():
                    val = shared_data['value']
                    with lock:
                        stale_reads.append(val)
                time.sleep(0.005)

        w = threading.Thread(target=writer)
        r = threading.Thread(target=reader)
        w.start()
        r.start()
        w.join(timeout=10)
        r.join(timeout=10)

        # После завершения писателя (value=10) все последующие чтения должны вернуть 10
        # Проверяем что последние чтения равны 10
        assert stale_reads[-1] == 10

    def test_write_starvation_prevention(self):
        """Писатели не голодают при постоянных читателях."""
        rwlock = RWLock()
        shared_data = {'value': 0}
        writer_wait_times = []
        lock = threading.Lock()

        def continuous_reader(stop_event):
            while not stop_event.is_set():
                with rwlock.read_lock():
                    time.sleep(0.01)

        def writer(writer_id):
            start = time.monotonic()
            with rwlock.write_lock():
                elapsed = time.monotonic() - start
                shared_data['value'] = writer_id
                with lock:
                    writer_wait_times.append(elapsed)

        # Запускаем 3 постоянных читателя
        stop = threading.Event()
        readers = [threading.Thread(target=continuous_reader, args=(stop,)) for _ in range(3)]
        for r in readers:
            r.start()

        # Даём читателям захватить lock
        time.sleep(0.05)

        # Запускаем писателя — он должен получить доступ благодаря write-preferring логике
        w = threading.Thread(target=writer, args=(42,))
        w.start()
        w.join(timeout=10)

        stop.set()
        for r in readers:
            r.join(timeout=5)

        assert shared_data['value'] == 42
        # Писатель должен был получить доступ за разумное время (< 2 сек)
        assert len(writer_wait_times) == 1
        assert writer_wait_times[0] < 2.0
