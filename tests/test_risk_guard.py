# tests/test_risk_guard.py

"""Unit-тесты для core/risk_guard.py"""

import threading
import time
from unittest.mock import patch

import pytest
from core.risk_guard import RiskGuard


class TestRiskGuardInit:
    """Тесты инициализации RiskGuard."""

    def test_default_init(self):
        """Проверяет начальное состояние."""
        guard = RiskGuard(strategy_id="test")
        assert guard.is_circuit_open() is False
        assert guard.consecutive_failures == 0
        assert guard.circuit_breaker_threshold == 3

    def test_custom_threshold(self):
        """Кастомный порог circuit breaker."""
        guard = RiskGuard(strategy_id="test", circuit_breaker_threshold=5)
        assert guard.circuit_breaker_threshold == 5


class TestCircuitBreaker:
    """Тесты circuit breaker."""

    def test_single_failure_does_not_open(self):
        """Одна ошибка не открывает circuit."""
        guard = RiskGuard(strategy_id="test", circuit_breaker_threshold=3)
        result = guard.record_failure()
        assert result is False
        assert guard.is_circuit_open() is False

    def test_threshold_failures_opens_circuit(self):
        """Достаточное количество ошибок открывает circuit."""
        guard = RiskGuard(strategy_id="test", circuit_breaker_threshold=3)
        guard.record_failure()
        guard.record_failure()
        result = guard.record_failure()
        assert result is True
        assert guard.is_circuit_open() is True

    def test_record_success_resets_counter(self):
        """record_success сбрасывает счётчик."""
        guard = RiskGuard(strategy_id="test", circuit_breaker_threshold=3)
        guard.record_failure()
        guard.record_failure()
        guard.record_success()
        assert guard.consecutive_failures == 0

    def test_success_after_failures_prevents_open(self):
        """Успех между ошибками предотвращает открытие."""
        guard = RiskGuard(strategy_id="test", circuit_breaker_threshold=3)
        guard.record_failure()
        guard.record_failure()
        guard.record_success()
        guard.record_failure()
        guard.record_failure()
        # После success счётчик сбросился, нужно ещё 3 ошибки
        assert guard.is_circuit_open() is False

    def test_reset_circuit_breaker(self):
        """reset_circuit_breaker полностью сбрасывает состояние."""
        guard = RiskGuard(strategy_id="test", circuit_breaker_threshold=3)
        guard.record_failure()
        guard.record_failure()
        guard.record_failure()
        assert guard.is_circuit_open() is True

        guard.reset_circuit_breaker()
        assert guard.is_circuit_open() is False
        assert guard.consecutive_failures == 0

    def test_circuit_timeout_resets_counter(self):
        """После timeout счётчик сбрасывается."""
        guard = RiskGuard(
            strategy_id="test",
            circuit_breaker_threshold=3,
            circuit_breaker_timeout=10.0,
        )
        fake_time = [100.0]
        with patch("core.risk_guard.time") as mock_time:
            mock_time.monotonic = lambda: fake_time[0]
            guard.record_failure()  # t=100.0
            guard.record_failure()  # t=100.0
            fake_time[0] = 111.0    # перепрыгиваем timeout
            # Следующая ошибка должна начать счёт заново
            result = guard.record_failure()
        assert result is False  # Это первая ошибка после timeout
        assert guard.consecutive_failures == 1


class TestRiskLimits:
    """Тесты проверки лимитов риска."""

    def test_no_limits_allows_all(self):
        """Без лимитов всё разрешено."""
        guard = RiskGuard(strategy_id="test")
        allowed, reason = guard.check_risk_limits("buy", 1000)
        assert allowed is True

    def test_max_position_size_blocks_large_order(self):
        """max_position_size блокирует большой ордер."""
        guard = RiskGuard(strategy_id="test", max_position_size=10)
        allowed, reason = guard.check_risk_limits("buy", 15)
        assert allowed is False
        assert "max_position_size" in reason

    def test_max_position_size_allows_small_order(self):
        """max_position_size пропускает маленький ордер."""
        guard = RiskGuard(strategy_id="test", max_position_size=10)
        allowed, reason = guard.check_risk_limits("buy", 5)
        assert allowed is True

    def test_max_position_size_ignores_close(self):
        """max_position_size не применяется к close."""
        guard = RiskGuard(strategy_id="test", max_position_size=10)
        allowed, reason = guard.check_risk_limits("close", 15)
        assert allowed is True

    def test_daily_loss_limit_blocks(self):
        """daily_loss_limit блокирует при достижении дневного лимита."""
        pnl_values = [0.0]  # мутабельная ячейка

        guard = RiskGuard(
            strategy_id="test",
            daily_loss_limit=1000.0,
            get_total_pnl=lambda sid: pnl_values[0],
        )
        # Первый вызов — фиксируем baseline = 0.0
        allowed, _ = guard.check_risk_limits("buy", 1)
        assert allowed is True

        # PnL упал на 1500 от baseline
        pnl_values[0] = -1500.0
        allowed, reason = guard.check_risk_limits("buy", 1)
        assert allowed is False
        assert "Дневной лимит" in reason

    def test_daily_loss_limit_allows(self):
        """daily_loss_limit пропускает если дневной убыток в пределах."""
        pnl_values = [0.0]

        guard = RiskGuard(
            strategy_id="test",
            daily_loss_limit=1000.0,
            get_total_pnl=lambda sid: pnl_values[0],
        )
        # Фиксируем baseline = 0
        guard.check_risk_limits("buy", 1)

        # PnL упал на 500 — ещё в пределах
        pnl_values[0] = -500.0
        allowed, reason = guard.check_risk_limits("buy", 1)
        assert allowed is True

    def test_daily_loss_limit_error_handling(self):
        """daily_loss_limit при ошибке расчёта уходит в fail-safe блокировку."""
        guard = RiskGuard(
            strategy_id="test",
            daily_loss_limit=1000.0,
            get_total_pnl=lambda sid: (_ for _ in ()).throw(Exception("DB error")),
        )
        with patch("core.risk_guard.runtime_metrics.emit_audit_event") as audit_mock:
            allowed, reason = guard.check_risk_limits("buy", 1)

        assert allowed is False
        assert "daily_loss_guard_error" in reason
        audit_mock.assert_called_once()
        assert audit_mock.call_args.args[0] == "risk_guard_daily_loss_error"


class TestBaselineEquityPolicy:
    """Тесты baseline equity policy для дневного лимита убытков (TASK-006)."""

    def test_baseline_set_on_first_check(self):
        """Baseline фиксируется при первом вызове check_risk_limits."""
        guard = RiskGuard(
            strategy_id="test",
            daily_loss_limit=500.0,
            get_total_pnl=lambda sid: -2000.0,  # уже в убытке с вчера
        )
        # При первом вызове baseline = -2000, daily_pnl = 0 → разрешено
        allowed, _ = guard.check_risk_limits("buy", 1)
        assert allowed is True
        assert guard._baseline_metric == -2000.0

    def test_restart_midday_preserves_baseline(self):
        """При рестарте в середине дня baseline берётся из текущего PnL."""
        pnl = [-3000.0]
        guard = RiskGuard(
            strategy_id="test",
            daily_loss_limit=1000.0,
            get_total_pnl=lambda sid: pnl[0],
        )
        # Фиксируем baseline = -3000
        guard.check_risk_limits("buy", 1)

        # PnL упал ещё на 800 → daily_pnl = -800, в пределах
        pnl[0] = -3800.0
        allowed, _ = guard.check_risk_limits("buy", 1)
        assert allowed is True

        # PnL упал ещё → daily_pnl = -1200, превышает
        pnl[0] = -4200.0
        allowed, reason = guard.check_risk_limits("buy", 1)
        assert allowed is False
        assert "baseline" in reason

    def test_new_day_resets_baseline(self):
        """При смене дня baseline пересчитывается."""
        pnl = [-1000.0]
        guard = RiskGuard(
            strategy_id="test",
            daily_loss_limit=500.0,
            get_total_pnl=lambda sid: pnl[0],
        )
        # День 1: baseline = -1000
        guard.check_risk_limits("buy", 1)
        assert guard._baseline_metric == -1000.0

        # Имитируем новый день — подменяем today_date
        guard._today_date = "1999-01-01"
        pnl[0] = -1200.0

        # Новый день: baseline = -1200, daily_pnl = 0
        allowed, _ = guard.check_risk_limits("buy", 1)
        assert allowed is True
        assert guard._baseline_metric == -1200.0

    def test_positive_pnl_day_not_blocked(self):
        """Прибыльный день не блокируется."""
        pnl = [0.0]
        guard = RiskGuard(
            strategy_id="test",
            daily_loss_limit=500.0,
            get_total_pnl=lambda sid: pnl[0],
        )
        guard.check_risk_limits("buy", 1)

        pnl[0] = 1000.0  # заработали
        allowed, _ = guard.check_risk_limits("buy", 1)
        assert allowed is True

    def test_open_loss_uses_current_equity(self):
        """Дневной лимит учитывает open loss через current equity, даже если realized PnL ещё ноль."""
        equity = [0.0]
        guard = RiskGuard(
            strategy_id="test",
            daily_loss_limit=500.0,
            get_total_pnl=lambda sid: 0.0,
            get_current_equity=lambda: equity[0],
        )

        allowed, _ = guard.check_risk_limits("buy", 1)
        assert allowed is True

        equity[0] = -700.0
        allowed, reason = guard.check_risk_limits("buy", 1)
        assert allowed is False
        assert "source=equity" in reason

    def test_equity_fallbacks_to_realized_pnl(self):
        """Если current equity недоступен, guard откатывается на realized PnL."""
        pnl = [0.0]
        guard = RiskGuard(
            strategy_id="test",
            daily_loss_limit=500.0,
            get_total_pnl=lambda sid: pnl[0],
            get_current_equity=lambda: None,
        )

        guard.check_risk_limits("buy", 1)
        pnl[0] = -600.0
        allowed, reason = guard.check_risk_limits("buy", 1)
        assert allowed is False
        assert "source=pnl" in reason

    def test_daily_loss_metric_and_rollover_execute_under_single_lock(self):
        """Чтение метрики и day rollover выполняются внутри одного критического сектора."""

        class InspectingLock:
            def __init__(self):
                self._lock = threading.Lock()
                self.acquired = False

            def __enter__(self):
                self._lock.acquire()
                self.acquired = True
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                self.acquired = False
                self._lock.release()

        inspecting_lock = InspectingLock()

        def _get_total_pnl(_strategy_id):
            assert inspecting_lock.acquired is True
            return -250.0

        guard = RiskGuard(
            strategy_id="test",
            daily_loss_limit=500.0,
            get_total_pnl=_get_total_pnl,
        )
        guard._lock = inspecting_lock

        allowed, _ = guard.check_risk_limits("buy", 1)

        assert allowed is True
        assert guard._baseline_metric == -250.0

    def test_per_instrument_limit_overrides_global_limit(self):
        guard = RiskGuard(
            strategy_id="test",
            max_position_size=10,
            per_instrument_limits={"SBER": {"max_position_size": 2}},
        )

        allowed, reason = guard.check_risk_limits("buy", 3, ticker="SBER")

        assert allowed is False
        assert "max_position_size=2" in reason

    def test_trade_frequency_blocks_after_limit(self):
        guard = RiskGuard(
            strategy_id="test",
            max_trades_per_window=2,
            trade_window_sec=60.0,
        )

        guard.notify_order_submitted("buy", ticker="SBER")
        guard.notify_order_submitted("close", ticker="SBER")
        allowed, reason = guard.check_risk_limits("buy", 1, ticker="SBER")

        assert allowed is False
        assert "trade frequency limit" in reason

    def test_cooldown_after_close_blocks_new_entry(self):
        guard = RiskGuard(
            strategy_id="test",
            cooldown_after_close_sec=30.0,
        )

        guard.notify_order_submitted("close", ticker="SBER")
        allowed, reason = guard.check_risk_limits("buy", 1, ticker="SBER")

        assert allowed is False
        assert "cooldown_after_close" in reason
