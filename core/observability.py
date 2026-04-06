from __future__ import annotations

from core.order_lifecycle import pending_order_registry
from core.runtime_metrics import runtime_metrics
from core.storage import get_all_strategies
from core.strategy_position_book import get_strategy_position_book


def _safe_broker_qty(connector, account_id: str, ticker: str) -> int | None:
    if connector is None or not connector.is_connected():
        return None
    try:
        positions = connector.get_positions(account_id) or []
    except Exception:
        return None
    for pos in positions:
        if str(pos.get("ticker", "")).upper() == str(ticker or "").upper():
            try:
                return int(float(pos.get("quantity", 0) or 0))
            except (TypeError, ValueError):
                return None
    return 0


def collect_strategies_health() -> dict:
    from core.autostart import get_all_runtime_states

    strategies = get_all_strategies()
    runtime_states = get_all_runtime_states()
    pending = pending_order_registry.get_pending()
    pending_by_strategy = {}
    for lifecycle in pending:
        pending_by_strategy.setdefault(lifecycle.strategy_id, 0)
        pending_by_strategy[lifecycle.strategy_id] += 1

    result = {}
    for sid, data in strategies.items():
        runtime = runtime_states.get(sid, {})
        result[sid] = {
            "desired_state": data.get("desired_state") or data.get("status", "stopped"),
            "actual_state": runtime.get("actual_state", "stopped"),
            "sync_status": runtime.get("sync_status", "unknown"),
            "is_running": runtime.get("is_running", False),
            "pending_orders_count": pending_by_strategy.get(sid, 0),
            "ticker": data.get("ticker") or data.get("params", {}).get("ticker", ""),
            "account_id": data.get("account_id") or data.get("finam_account", ""),
        }
    return result


def collect_runtime_metrics() -> dict:
    from core.autostart import get_all_runtime_states
    from core.connector_manager import connector_manager

    strategies = get_all_strategies()
    runtime_states = get_all_runtime_states()
    strategy_health = collect_strategies_health()
    broker_vs_history_qty = {}
    broker_vs_engine_qty = {}

    for sid, data in strategies.items():
        account_id = data.get("account_id") or data.get("finam_account", "")
        ticker = data.get("ticker") or data.get("params", {}).get("ticker", "")
        connector_id = data.get("connector_id") or data.get("connector") or "finam"
        connector = connector_manager.get(connector_id)
        broker_qty = _safe_broker_qty(connector, account_id, ticker)

        history_entries = get_strategy_position_book(sid, ticker=ticker) if ticker else []
        history_qty = sum(int(entry.get("quantity", 0) or 0) for entry in history_entries)
        runtime = runtime_states.get(sid, {})

        broker_vs_history_qty[sid] = None if broker_qty is None else broker_qty - history_qty
        broker_vs_engine_qty[sid] = {
            "broker_qty": broker_qty,
            "runtime_state": runtime.get("actual_state", "stopped"),
            "sync_status": runtime.get("sync_status", "unknown"),
        }

    snapshot = runtime_metrics.snapshot()
    counters = snapshot.get("counters", {})
    return {
        "drift": {
            "broker_vs_engine_qty": broker_vs_engine_qty,
            "broker_vs_history_qty": broker_vs_history_qty,
            "pending_orders_count": len(pending_order_registry.get_pending()),
            "stale_state_count": sum(
                1 for info in strategy_health.values() if info.get("sync_status") == "stale"
            ),
        },
        "latency": snapshot.get("latencies", {}),
        "counters": counters,
        "audit_events": snapshot.get("audit_events", []),
    }


def collect_health_snapshot() -> dict:
    from core.connector_manager import connector_manager

    connectors = {}
    for connector_id, connector in connector_manager.all().items():
        connectors[connector_id] = {
            "connected": bool(connector and connector.is_connected()),
        }
    return {
        "status": "ok",
        "connectors": connectors,
        "strategies": collect_strategies_health(),
        "metrics": collect_runtime_metrics(),
    }