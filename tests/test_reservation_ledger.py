# tests/test_reservation_ledger.py

"""Unit-тесты для core/reservation_ledger.py."""

import time
from unittest.mock import patch

from core.reservation_ledger import ReservationLedger


class TestReservationLedger:
    """Базовые операции reserve / release / available."""

    def test_reserve_and_total(self):
        ledger = ReservationLedger()
        ledger.reserve("s1:Si:1", "acc1", 10000.0)
        assert ledger.total_reserved("acc1") == 10000.0
        assert ledger.total_reserved("acc2") == 0.0

    def test_multiple_reserves_sum(self):
        ledger = ReservationLedger()
        ledger.reserve("s1:Si:1", "acc1", 10000.0)
        ledger.reserve("s2:Si:2", "acc1", 15000.0)
        assert ledger.total_reserved("acc1") == 25000.0

    def test_release_removes_reservation(self):
        ledger = ReservationLedger()
        ledger.reserve("s1:Si:1", "acc1", 10000.0)
        ledger.release("s1:Si:1")
        assert ledger.total_reserved("acc1") == 0.0

    def test_release_nonexistent_key_no_error(self):
        ledger = ReservationLedger()
        ledger.release("nonexistent")  # не должно бросить исключение

    def test_available_subtracts_reserved(self):
        ledger = ReservationLedger()
        ledger.reserve("s1:Si:1", "acc1", 10000.0)
        assert ledger.available("acc1", 50000.0) == 40000.0

    def test_available_floor_zero(self):
        ledger = ReservationLedger()
        ledger.reserve("s1:Si:1", "acc1", 60000.0)
        assert ledger.available("acc1", 50000.0) == 0.0

    def test_available_no_reservations(self):
        ledger = ReservationLedger()
        assert ledger.available("acc1", 50000.0) == 50000.0

    def test_accounts_isolated(self):
        ledger = ReservationLedger()
        ledger.reserve("s1:Si:1", "acc1", 10000.0)
        ledger.reserve("s2:Si:2", "acc2", 20000.0)
        assert ledger.total_reserved("acc1") == 10000.0
        assert ledger.total_reserved("acc2") == 20000.0
        assert ledger.available("acc1", 50000.0) == 40000.0
        assert ledger.available("acc2", 50000.0) == 30000.0


class TestStaleEviction:
    """Устаревшие резервы исключаются из капитала и затем очищаются."""

    def test_stale_reservation_excluded_from_total_reserved(self):
        ledger = ReservationLedger(stale_timeout_sec=1.0, stale_cleanup_sec=60.0)
        ledger.reserve("s1:Si:1", "acc1", 10000.0)

        # Подменяем ts на давнее время
        with ledger._lock:
            ledger._reservations["s1:Si:1"]["ts"] = time.monotonic() - 2.0

        assert ledger.total_reserved("acc1") == 0.0
        snapshot = ledger.snapshot()["s1:Si:1"]
        assert snapshot["stale"] is True
        assert snapshot["stale_reason"] == "timeout"

    def test_non_stale_preserved(self):
        ledger = ReservationLedger(stale_timeout_sec=300.0)
        ledger.reserve("s1:Si:1", "acc1", 10000.0)
        assert ledger.total_reserved("acc1") == 10000.0

    def test_mark_stale_excludes_reservation_before_cleanup(self):
        ledger = ReservationLedger(stale_timeout_sec=300.0, stale_cleanup_sec=60.0)
        ledger.reserve("s1:Si:1", "acc1", 10000.0)

        assert ledger.mark_stale("s1:Si:1", "ambiguous_submit") is True

        assert ledger.total_reserved("acc1") == 0.0
        snapshot = ledger.snapshot()["s1:Si:1"]
        assert snapshot["stale"] is True
        assert snapshot["stale_reason"] == "ambiguous_submit"

    def test_stale_cleanup_removes_reservation_and_emits_audit(self):
        ledger = ReservationLedger(stale_timeout_sec=300.0, stale_cleanup_sec=1.0)
        ledger.reserve("s1:Si:1", "acc1", 10000.0)

        with ledger._lock:
            ledger._reservations["s1:Si:1"]["stale"] = True
            ledger._reservations["s1:Si:1"]["stale_reason"] = "timeout"
            ledger._reservations["s1:Si:1"]["stale_marked_at"] = time.monotonic() - 2.0

        with patch("core.reservation_ledger.runtime_metrics.emit_audit_event") as audit_mock:
            assert ledger.total_reserved("acc1") == 0.0

        assert ledger.snapshot() == {}
        audit_mock.assert_called_once()
        assert audit_mock.call_args.args[0] == "stale_reservation_cleanup"
        assert audit_mock.call_args.kwargs["reservation_key"] == "s1:Si:1"


class TestReserveOverwrite:
    """Повторный reserve по тому же ключу перезаписывает."""

    def test_overwrite_updates_amount(self):
        ledger = ReservationLedger()
        ledger.reserve("s1:Si:1", "acc1", 10000.0)
        ledger.reserve("s1:Si:1", "acc1", 20000.0)
        assert ledger.total_reserved("acc1") == 20000.0
