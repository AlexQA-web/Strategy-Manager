from core.fill_ledger import FillLedger


class TestFillLedger:
    def test_duplicate_fill_is_durable_via_projection_statuses(self, monkeypatch):
        ledger = FillLedger()

        order_statuses = iter(["duplicate"])
        trade_statuses = iter(["duplicate"])

        monkeypatch.setattr("core.fill_ledger.save_order", lambda order: next(order_statuses))
        monkeypatch.setattr("core.fill_ledger.append_trade", lambda trade: next(trade_statuses))

        result = ledger.record_fill(
            fill_id="fill-1",
            strategy_id="agent-1",
            ticker="SBER",
            board="TQBR",
            side="buy",
            qty=1,
            price=100.0,
        )

        assert result.is_duplicate is True
        assert result.error == ""

    def test_projection_repair_is_explicit(self, monkeypatch):
        ledger = FillLedger()

        monkeypatch.setattr("core.fill_ledger.save_order", lambda order: "duplicate")
        monkeypatch.setattr("core.fill_ledger.append_trade", lambda trade: "inserted")

        result = ledger.record_fill(
            fill_id="fill-2",
            strategy_id="agent-1",
            ticker="SBER",
            board="TQBR",
            side="sell",
            qty=1,
            price=100.0,
        )

        assert result.is_repair is True
        assert result.is_success is True

    def test_projection_error_stays_explicit(self, monkeypatch):
        ledger = FillLedger()

        monkeypatch.setattr("core.fill_ledger.save_order", lambda order: "inserted")

        def _raise(trade):
            raise RuntimeError("disk full")

        monkeypatch.setattr("core.fill_ledger.append_trade", _raise)

        result = ledger.record_fill(
            fill_id="fill-3",
            strategy_id="agent-1",
            ticker="SBER",
            board="TQBR",
            side="buy",
            qty=2,
            price=101.0,
        )

        assert result.is_success is False
        assert result.order_status == "inserted"
        assert result.trade_status == "error"
        assert "disk full" in result.error

    def test_correlation_id_propagates_to_both_projections(self, monkeypatch):
        ledger = FillLedger()
        captured = {}

        def _save_order(order):
            captured["order"] = order
            return "inserted"

        def _append_trade(trade):
            captured["trade"] = trade
            return "inserted"

        monkeypatch.setattr("core.fill_ledger.save_order", _save_order)
        monkeypatch.setattr("core.fill_ledger.append_trade", _append_trade)

        result = ledger.record_fill(
            fill_id="fill-4",
            strategy_id="agent-1",
            ticker="SBER",
            board="TQBR",
            side="buy",
            qty=1,
            price=100.0,
            correlation_id="corr-123",
        )

        assert result.is_success is True
        assert captured["order"]["correlation_id"] == "corr-123"
        assert captured["trade"]["correlation_id"] == "corr-123"

    def test_processing_reservation_blocks_parallel_duplicate_before_io_finishes(self, monkeypatch):
        import threading

        ledger = FillLedger()
        started = threading.Event()
        release = threading.Event()
        save_calls = []
        thread_result = {}

        def _save_order(order):
            save_calls.append(order["exec_key"])
            started.set()
            release.wait(timeout=1)
            return "inserted"

        monkeypatch.setattr("core.fill_ledger.save_order", _save_order)
        monkeypatch.setattr("core.fill_ledger.append_trade", lambda trade: "inserted")

        def _record_in_thread():
            thread_result["result"] = ledger.record_fill(
                fill_id="fill-race",
                strategy_id="agent-1",
                ticker="SBER",
                board="TQBR",
                side="buy",
                qty=1,
                price=100.0,
            )

        worker = threading.Thread(target=_record_in_thread)
        worker.start()
        assert started.wait(timeout=1) is True

        duplicate = ledger.record_fill(
            fill_id="fill-race",
            strategy_id="agent-1",
            ticker="SBER",
            board="TQBR",
            side="buy",
            qty=1,
            price=100.0,
        )

        release.set()
        worker.join(timeout=1)

        assert thread_result["result"].is_success is True
        assert duplicate.is_duplicate is True
        assert save_calls == ["fill-race"]

    def test_processing_reservation_is_released_after_projection_error(self, monkeypatch):
        ledger = FillLedger()
        order_results = iter(["inserted", "duplicate"])
        append_results = iter([RuntimeError("disk full"), "inserted"])

        monkeypatch.setattr("core.fill_ledger.save_order", lambda order: next(order_results))

        def _append_trade(trade):
            outcome = next(append_results)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr("core.fill_ledger.append_trade", _append_trade)

        first = ledger.record_fill(
            fill_id="fill-retry",
            strategy_id="agent-1",
            ticker="SBER",
            board="TQBR",
            side="buy",
            qty=1,
            price=100.0,
        )
        second = ledger.record_fill(
            fill_id="fill-retry",
            strategy_id="agent-1",
            ticker="SBER",
            board="TQBR",
            side="buy",
            qty=1,
            price=100.0,
        )

        assert first.is_success is False
        assert "disk full" in first.error
        assert second.is_repair is True