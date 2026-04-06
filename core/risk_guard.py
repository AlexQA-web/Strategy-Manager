# core/risk_guard.py

"""
Risk Guard — защита от рисков и circuit breaker.

Инкапсулирует:
- Circuit breaker логику (_consecutive_failures, _circuit_open)
- Проверку лимитов риска (max_position_size, daily_loss_limit)
"""

import threading
import time
from collections import deque
from typing import Optional

from loguru import logger

from core.runtime_metrics import runtime_metrics


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
        per_instrument_limits: Optional[dict] = None,
        max_trades_per_window: int = 0,
        trade_window_sec: float = 60.0,
        cooldown_after_close_sec: float = 0.0,
    ):
        self._strategy_id = strategy_id
        self._circuit_breaker_threshold = circuit_breaker_threshold
        self._circuit_breaker_timeout = circuit_breaker_timeout
        self._max_position_size = max_position_size
        self._daily_loss_limit = daily_loss_limit
        self._get_total_pnl = get_total_pnl
        self._get_current_equity = get_current_equity
        self._per_instrument_limits = dict(per_instrument_limits or {})
        self._max_trades_per_window = max(int(max_trades_per_window or 0), 0)
        self._trade_window_sec = max(float(trade_window_sec or 0.0), 0.0)
        self._cooldown_after_close_sec = max(float(cooldown_after_close_sec or 0.0), 0.0)

        self._lock = threading.Lock()
        self._consecutive_failures: int = 0
        self._last_failure_time: float = 0.0
        self._circuit_open: bool = False

        # Дневной лимит убытков — baseline metric policy.
        # Предпочитаем current equity, fallback — realized PnL.
        self._today_date: str = ""
        self._baseline_metric: float = 0.0
        self._baseline_source: str = "pnl"
        self._trade_events: dict[str, deque[float]] = {}
        self._last_close_ts: dict[str, float] = {}

    @staticmethod
    def _normalize_ticker(ticker: str) -> str:
        return str(ticker or "").strip().upper()

    def _get_instrument_profile(self, ticker: str) -> dict:
        normalized = self._normalize_ticker(ticker)
        if not normalized:
            return {}
        profile = self._per_instrument_limits.get(normalized, {})
        return profile if isinstance(profile, dict) else {}

    @staticmethod
    def _prune_trade_events(events: deque[float], now: float, window_sec: float) -> None:
        if window_sec <= 0:
            events.clear()
            return
        cutoff = now - window_sec
        while events and events[0] < cutoff:
            events.popleft()

    def notify_order_submitted(self, action: str, ticker: str = "") -> None:
        normalized_ticker = self._normalize_ticker(ticker)
        if action not in ("buy", "sell", "close"):
            return
        with self._lock:
            now = time.monotonic()
            events = self._trade_events.setdefault(normalized_ticker, deque())
            self._prune_trade_events(events, now, self._trade_window_sec)
            events.append(now)
            if action == "close":
                self._last_close_ts[normalized_ticker] = now

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

    def check_risk_limits(self, action: str, qty: int, ticker: str = "") -> tuple[bool, str]:
        """Проверяет лимиты риска перед исполнением ордера.

        Returns:
            (allowed, reason): True если ордер разрешён, причина отказа если нет.
        """
        profile = self._get_instrument_profile(ticker)
        max_position_size = int(profile.get("max_position_size", self._max_position_size) or 0)
        daily_loss_limit = float(profile.get("daily_loss_limit", self._daily_loss_limit) or 0.0)
        max_trades_per_window = int(
            profile.get("max_trades_per_window", self._max_trades_per_window) or 0
        )
        trade_window_sec = float(profile.get("trade_window_sec", self._trade_window_sec) or 0.0)
        cooldown_after_close_sec = float(
            profile.get("cooldown_after_close_sec", self._cooldown_after_close_sec) or 0.0
        )
        normalized_ticker = self._normalize_ticker(ticker)

        # 1. Лимит размера позиции
        if max_position_size > 0 and action in ("buy", "sell"):
            if qty > max_position_size:
                return False, (
                    f"qty={qty} превышает max_position_size={max_position_size}"
                )

        # 2. Guard против rapid cycling и burst submit
        if action in ("buy", "sell") and (max_trades_per_window > 0 or cooldown_after_close_sec > 0):
            with self._lock:
                now = time.monotonic()
                if cooldown_after_close_sec > 0:
                    last_close_ts = self._last_close_ts.get(normalized_ticker, 0.0)
                    if last_close_ts and now - last_close_ts < cooldown_after_close_sec:
                        return False, (
                            f"cooldown_after_close active: {now - last_close_ts:.2f}s "
                            f"< {cooldown_after_close_sec:.2f}s"
                        )
                if max_trades_per_window > 0 and trade_window_sec > 0:
                    events = self._trade_events.setdefault(normalized_ticker, deque())
                    self._prune_trade_events(events, now, trade_window_sec)
                    if len(events) >= max_trades_per_window:
                        return False, (
                            f"trade frequency limit reached: {len(events)} trades in "
                            f"{trade_window_sec:.0f}s (limit={max_trades_per_window})"
                        )

        # 3. Дневной лимит убытков (baseline metric policy)
        if daily_loss_limit > 0 and (self._get_current_equity or self._get_total_pnl):
            try:
                daily_pnl, current_metric, metric_source = self._evaluate_daily_loss_locked()
                if daily_pnl < -abs(daily_loss_limit):
                    return False, (
                        f"Дневной лимит убытков достигнут: "
                        f"daily_pnl={daily_pnl:.2f} "
                        f"(baseline={self._baseline_metric:.2f}, "
                        f"current={float(current_metric):.2f}, source={metric_source}), "
                        f"limit={daily_loss_limit:.2f}"
                    )
            except Exception as exc:
                runtime_metrics.increment("risk_guard_daily_loss_error")
                runtime_metrics.emit_audit_event(
                    "risk_guard_daily_loss_error",
                    strategy_id=self._strategy_id,
                    error=str(exc),
                )
                logger.error(
                    f"[{self._strategy_id}] daily loss guard error, block new entries: {exc}"
                )
                return False, f"daily_loss_guard_error: {exc}"

        return True, ""

    def _evaluate_daily_loss_locked(self) -> tuple[float, float, str]:
        """Атомарно читает текущую метрику, выполняет day rollover и считает daily PnL."""
        from datetime import date

        with self._lock:
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

            current_metric_value = float(current_metric)
            if today != self._today_date:
                self._today_date = today
                self._baseline_metric = current_metric_value
                self._baseline_source = metric_source

            daily_pnl = current_metric_value - self._baseline_metric
            return daily_pnl, current_metric_value, metric_source

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
