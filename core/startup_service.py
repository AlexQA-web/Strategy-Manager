"""Preflight startup snapshot service."""

from __future__ import annotations

from loguru import logger

from core.order_lifecycle import pending_order_registry
from core.runtime_metrics import runtime_metrics


def fetch_startup_snapshot(connector, account_id: str, strategy_id: str) -> dict:
    """Загружает best-effort snapshot позиций, pending orders и баланса до старта."""
    snapshot = {
        "positions": [],
        "accounts": [],
        "balance": None,
        "pending_orders": [],
        "pending_recovery": {"recovered": [], "unresolved": []},
    }

    snapshot["positions"] = connector.get_positions(account_id) or []

    try:
        snapshot["accounts"] = connector.get_accounts() or []
    except Exception as exc:
        logger.debug(f"[Startup:{strategy_id}] get_accounts failed: {exc}")

    try:
        snapshot["balance"] = connector.get_free_money(account_id)
    except Exception as exc:
        logger.debug(f"[Startup:{strategy_id}] get_free_money failed: {exc}")

    snapshot["pending_orders"] = [
        lifecycle.snapshot()
        for lifecycle in pending_order_registry.get_pending()
        if lifecycle.strategy_id == strategy_id
    ]
    if snapshot["pending_orders"]:
        snapshot["pending_recovery"] = pending_order_registry.recover_strategy_orders(
            connector,
            strategy_id,
        )
    runtime_metrics.emit_audit_event(
        "startup_snapshot",
        strategy_id=strategy_id,
        account_id=account_id,
        pending_orders_count=len(snapshot["pending_orders"]),
        unresolved_pending=len(snapshot["pending_recovery"].get("unresolved", [])),
        positions_count=len(snapshot["positions"]),
    )
    return snapshot
