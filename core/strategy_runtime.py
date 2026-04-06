"""Runtime state machine для стратегии."""

from __future__ import annotations

from enum import Enum


class StrategyRuntimeState(str, Enum):
    INITIALIZING = "initializing"
    SYNCED = "synced"
    STALE = "stale"
    TRADING = "trading"
    DEGRADED = "degraded"
    MANUAL_INTERVENTION_REQUIRED = "manual_intervention_required"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED_START = "failed_start"


ALLOWED_RUNTIME_TRANSITIONS: dict[str, set[str]] = {
    StrategyRuntimeState.STOPPED.value: {
        StrategyRuntimeState.INITIALIZING.value,
        StrategyRuntimeState.STOPPED.value,
    },
    StrategyRuntimeState.INITIALIZING.value: {
        StrategyRuntimeState.SYNCED.value,
        StrategyRuntimeState.STALE.value,
        StrategyRuntimeState.DEGRADED.value,
        StrategyRuntimeState.TRADING.value,
        StrategyRuntimeState.FAILED_START.value,
        StrategyRuntimeState.STOPPING.value,
    },
    StrategyRuntimeState.SYNCED.value: {
        StrategyRuntimeState.TRADING.value,
        StrategyRuntimeState.DEGRADED.value,
        StrategyRuntimeState.STALE.value,
        StrategyRuntimeState.STOPPING.value,
    },
    StrategyRuntimeState.TRADING.value: {
        StrategyRuntimeState.SYNCED.value,
        StrategyRuntimeState.STALE.value,
        StrategyRuntimeState.DEGRADED.value,
        StrategyRuntimeState.MANUAL_INTERVENTION_REQUIRED.value,
        StrategyRuntimeState.STOPPING.value,
    },
    StrategyRuntimeState.STALE.value: {
        StrategyRuntimeState.SYNCED.value,
        StrategyRuntimeState.DEGRADED.value,
        StrategyRuntimeState.MANUAL_INTERVENTION_REQUIRED.value,
        StrategyRuntimeState.STOPPING.value,
    },
    StrategyRuntimeState.DEGRADED.value: {
        StrategyRuntimeState.SYNCED.value,
        StrategyRuntimeState.TRADING.value,
        StrategyRuntimeState.MANUAL_INTERVENTION_REQUIRED.value,
        StrategyRuntimeState.STOPPING.value,
    },
    StrategyRuntimeState.MANUAL_INTERVENTION_REQUIRED.value: {
        StrategyRuntimeState.SYNCED.value,
        StrategyRuntimeState.DEGRADED.value,
        StrategyRuntimeState.STOPPING.value,
    },
    StrategyRuntimeState.FAILED_START.value: {
        StrategyRuntimeState.INITIALIZING.value,
        StrategyRuntimeState.STOPPED.value,
    },
    StrategyRuntimeState.STOPPING.value: {
        StrategyRuntimeState.STOPPED.value,
    },
}


def is_valid_runtime_transition(current_state: str, new_state: str) -> bool:
    """Проверяет допустимость перехода runtime state."""
    if current_state == new_state:
        return True
    return new_state in ALLOWED_RUNTIME_TRANSITIONS.get(current_state, set())
