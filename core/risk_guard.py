# core/risk_guard.py

"""
Risk Guard — защита от рисков и circuit breaker.

Инкапсулирует:
- Circuit breaker логику (_consecutive_failures, _circuit_open)
- Проверку лимитов риска (max_position_size, daily_loss_limit)
"""

import threading
import time
from typing import Optional


class RiskGuard:
    """Circuit breaker и проверка лимитов риска.

    Circuit breaker:
        - Считает последовательные ошибки
        - При достижении порога — открывает цепь (stop trading)
        - Через timeout — сбрасывает счётчик

    Risk limits:
        - max_position_size: лимит размера позиции
        - daily_loss_limit: дневной лимит убытков
    """

    def __init__(
        self,
        strategy_id: str,
        circuit_breaker_threshold: int = 3,
        circuit_breaker_timeout: float = 60.0,
        max_position_size: int = 0,
        daily_loss_limit: float = 0.0,
        get_total_pnl=None,
        get_current_equity=None,
    ):
        self._strategy_id = strategy_id
        self._circuit_breaker_threshold = circuit_breaker_threshold
        self._circuit_breaker_timeout = circuit_breaker_timeout
        self._max_position_size = max_position_size
        self._daily_loss_limit = daily_loss_limit
        self._get_total_pnl = get_total_pnl
        self._get_current_equity = get_current_equity

        self._lock = threading.Lock()
        self._consecutive_failures: int = 0
        self._last_failure_time: float = 0.0
        self._circuit_open: bool = False

        # Дневной лимит убытков — baseline metric policy.
        # Предпочитаем current equity, fallback — realized PnL.
        self._today_date: str = ""
        self._baseline_metric: float = 0.0
        self._baseline_source: str = "pnl"

    # --- Circuit breaker ---

    def is_circuit_open(self) -> bool:
        """Возвращает True, если circuit breaker активен."""
        with self._lock:
            return self._circuit_open

    def record_success(self):
        """Сбрасывает счётчик ошибок при успешной операции."""
        with self._lock:
            self._consecutive_failures = 0

    def record_failure(self) -> bool:
        """Регистрирует ошибку. Возвращает True, если circuit breaker сработал."""
        with self._lock:
            now = time.monotonic()
            if now - self._last_failure_time < self._circuit_breaker_timeout:
                self._consecutive_failures += 1
            else:
                self._consecutive_failures = 1
            self._last_failure_time = now

            if self._consecutive_failures >= self._circuit_breaker_threshold:
                self._circuit_open = True
                return True
            return False

    def reset_circuit_breaker(self):
        """Сбрасывает circuit breaker вручную."""
        with self._lock:
            self._consecutive_failures = 0
            self._last_failure_time = 0.0
            self._circuit_open = False

    def get_failure_count(self) -> int:
        """Возвращает текущий счётчик последовательных ошибок."""
        with self._lock:
            return self._consecutive_failures

    # --- Risk limits ---

    def check_risk_limits(self, action: str, qty: int) -> tuple[bool, str]:
        """Проверяет лимиты риска перед исполнением ордера.

        Returns:
            (allowed, reason): True если ордер разрешён, причина отказа если нет.
        """
        # 1. Лимит размера позиции
        if self._max_position_size > 0 and action in ("buy", "sell"):
            if qty > self._max_position_size:
                return False, (
                    f"qty={qty} превышает max_position_size={self._max_position_size}"
                )

        # 2. Дневной лимит убытков (baseline metric policy)
        if self._daily_loss_limit > 0 and (self._get_current_equity or self._get_total_pnl):
            try:
                from datetime import date
                today = date.today().isoformat()

                current_metric = None
                metric_source = "pnl"

                if self._get_current_equity:
                    current_metric = self._get_current_equity()
                    if current_metric is not None:
                        metric_source = "equity"

                if current_metric is None:
                    current_metric = self._get_total_pnl(self._strategy_id) or 0.0
                    metric_source = "pnl"

                if today != self._today_date:
                    # Новый день (или первый вызов / рестарт) — фиксируем baseline.
                    self._today_date = today
                    self._baseline_metric = float(current_metric)
                    self._baseline_source = metric_source

                daily_pnl = float(current_metric) - self._baseline_metric
                if daily_pnl < -abs(self._daily_loss_limit):
                    return False, (
                        f"Дневной лимит убытков достигнут: "
                        f"daily_pnl={daily_pnl:.2f} "
                        f"(baseline={self._baseline_metric:.2f}, "
                        f"current={float(current_metric):.2f}, source={metric_source}), "
                        f"limit={self._daily_loss_limit:.2f}"
                    )
            except Exception:
                pass

        return True, ""

    # --- Свойства ---

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures

    @property
    def circuit_open(self) -> bool:
        with self._lock:
            return self._circuit_open

    @property
    def circuit_breaker_threshold(self) -> int:
        return self._circuit_breaker_threshold
