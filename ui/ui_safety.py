from __future__ import annotations

import time


class DestructiveActionGuard:
    def __init__(self, debounce_window_sec: float = 1.0):
        self._active = False
        self._debounce_window_sec = max(float(debounce_window_sec or 0.0), 0.0)
        self._last_completed_ts = 0.0

    @property
    def active(self) -> bool:
        return self._active

    def run(self, widgets, action):
        if self._debounce_window_sec > 0:
            now = time.monotonic()
            if now - self._last_completed_ts < self._debounce_window_sec:
                return False
        if self._active:
            return False
        self._active = True
        widgets = [widget for widget in widgets if widget is not None]
        try:
            for widget in widgets:
                widget.setEnabled(False)
            action()
            return True
        finally:
            for widget in widgets:
                widget.setEnabled(True)
            self._active = False
            self._last_completed_ts = time.monotonic()


def format_runtime_status(desired_state: str, actual_state: str, sync_status: str) -> tuple[str, str]:
    status_map = {
        "trading": (f"{desired_state} · trading · {sync_status}", "#a6e3a1"),
        "synced": (f"{desired_state} · synced · {sync_status}", "#89b4fa"),
        "degraded": (f"{desired_state} · degraded · {sync_status}", "#f9e2af"),
        "stale": (f"{desired_state} · stale · {sync_status}", "#f9e2af"),
        "manual_intervention_required": (f"{desired_state} · manual intervention", "#f38ba8"),
        "failed_start": (f"{desired_state} · failed start", "#f38ba8"),
        "stopped": (f"{desired_state} · stopped · {sync_status}", "#f38ba8"),
    }
    return status_map.get(
        actual_state,
        (f"{desired_state} · {actual_state} · {sync_status}", "#6c7086"),
    )


def build_strategy_close_confirmation(strategy_id: str, ticker: str | None = None, quantity: int = 0) -> str:
    suffix = f" по {ticker}" if ticker else ""
    qty_text = f" Объём: {quantity}." if quantity > 0 else ""
    return (
        f"Закрыть позицию стратегии {strategy_id}{suffix}?\n"
        f"Будут закрыты только лоты этой стратегии.{qty_text}"
    )


def build_strategy_stop_confirmation(strategy_id: str) -> str:
    return (
        f"Остановить стратегию {strategy_id}?\n"
        "Если у стратегии есть открытая позиция, она не будет закрыта автоматически."
    )


def build_account_close_confirmation() -> str:
    return "Закрыть ВСЕ открытые позиции счёта по рыночной цене? Это account-level действие."