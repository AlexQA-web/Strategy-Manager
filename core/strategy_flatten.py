"""Strategy-scoped flatten planner и executor."""

from __future__ import annotations

import time
from typing import Callable, Optional

from loguru import logger

from core.connector_manager import connector_manager
from core.runtime_metrics import runtime_metrics
from core.storage import get_strategy
from core.strategy_position_book import get_strategy_position, get_strategy_position_book


_TERMINAL_ORDER_STATUSES = {
    "matched", "cancelled", "canceled", "denied", "removed", "expired", "killed",
}


def build_strategy_flatten_plan(
    strategy_id: str,
    ticker: Optional[str] = None,
    board: Optional[str] = None,
    quantity: int = 0,
) -> dict:
    """Строит strategy-scoped план закрытия на основе position book."""
    data = get_strategy(strategy_id) or {}
    account_id = data.get("account_id") or data.get("finam_account", "")
    connector_id = data.get("connector_id") or data.get("connector") or "finam"

    entries = get_strategy_position_book(strategy_id, ticker=ticker, board=board)
    items: list[dict] = []
    for entry in entries:
        open_qty = int(entry["quantity"])
        if open_qty <= 0:
            continue
        close_qty = open_qty
        if quantity > 0:
            close_qty = min(quantity, open_qty)
        items.append({
            "strategy_id": strategy_id,
            "account_id": account_id,
            "connector_id": connector_id,
            "ticker": entry["ticker"],
            "board": entry["board"],
            "position_side": entry["side"],
            "open_qty": open_qty,
            "close_qty": close_qty,
            "close_side": "sell" if entry["side"] == "buy" else "buy",
            "avg_entry_price": float(entry["avg_entry_price"]),
            "open_lots": list(entry["open_lots"]),
        })
        if quantity > 0:
            break

    return {
        "strategy_id": strategy_id,
        "account_id": account_id,
        "connector_id": connector_id,
        "items": items,
    }


class StrategyFlattenExecutor:
    """Безопасное strategy-scoped закрытие позиции по истории стратегии."""

    def __init__(
        self,
        connector_resolver: Callable[[str], object] | None = None,
        position_reader: Callable[..., dict] | None = None,
        sleep_func: Callable[[float], None] | None = None,
    ):
        self._connector_resolver = connector_resolver or connector_manager.get
        self._position_reader = position_reader or get_strategy_position
        self._sleep = sleep_func or time.sleep

    def execute(
        self,
        strategy_id: str,
        ticker: Optional[str] = None,
        board: Optional[str] = None,
        quantity: int = 0,
        wait_for_confirmation: bool = False,
        timeout_sec: float = 10.0,
        poll_interval: float = 0.5,
        max_child_orders: int = 3,
    ) -> dict:
        plan = build_strategy_flatten_plan(strategy_id, ticker=ticker, board=board, quantity=quantity)
        runtime_metrics.emit_audit_event(
            "flatten_requested",
            strategy_id=strategy_id,
            ticker=ticker or "",
            board=board or "",
            quantity=quantity,
            items=len(plan["items"]),
        )
        if not plan["items"]:
            return {
                "status": "no_position",
                "strategy_id": strategy_id,
                "plan": plan,
                "items": [],
            }

        connector = self._connector_resolver(plan["connector_id"])
        if connector is None or not connector.is_connected():
            return {
                "status": "connector_unavailable",
                "strategy_id": strategy_id,
                "plan": plan,
                "items": [],
            }

        results: list[dict] = []
        overall_status = "submitted"
        for item in plan["items"]:
            validation = self._validate_broker_position(connector, item)
            if not validation["ok"]:
                result = {
                    "status": "manual_intervention_required",
                    "reason": validation["reason"],
                    **item,
                }
                results.append(result)
                overall_status = "manual_intervention_required"
                runtime_metrics.emit_audit_event(
                    "flatten_manual_intervention",
                    strategy_id=strategy_id,
                    ticker=item["ticker"],
                    reason=validation["reason"],
                )
                continue

            if not wait_for_confirmation:
                close_result = connector.close_position_result(
                    account_id=item["account_id"],
                    ticker=item["ticker"],
                    quantity=item["close_qty"],
                    agent_name=strategy_id,
                )
                order_id = close_result.transaction_id
                if not order_id:
                    logger.error(
                        f"[StrategyFlatten:{strategy_id}] close_position rejected: "
                        f"{item['ticker']} x{item['close_qty']} "
                        f"outcome={close_result.outcome.value} msg={close_result.message}"
                    )
                    results.append({
                        "status": "manual_intervention_required",
                        "reason": close_result.outcome.value,
                        "order_id": None,
                        **item,
                    })
                    overall_status = "manual_intervention_required"
                    runtime_metrics.emit_audit_event(
                        "flatten_manual_intervention",
                        strategy_id=strategy_id,
                        ticker=item["ticker"],
                        reason=close_result.outcome.value,
                    )
                    continue
                results.append({
                    "status": "submitted",
                    "order_id": order_id,
                    **item,
                })
                runtime_metrics.emit_audit_event(
                    "flatten_submitted",
                    strategy_id=strategy_id,
                    ticker=item["ticker"],
                    order_id=order_id,
                    quantity=item["close_qty"],
                )
                continue

            confirmation = self._flatten_until_target(
                connector=connector,
                strategy_id=strategy_id,
                item=item,
                timeout_sec=timeout_sec,
                poll_interval=poll_interval,
                max_child_orders=max_child_orders,
            )
            item_result = {**item, **confirmation}
            if confirmation["status"] != "success":
                overall_status = "manual_intervention_required"
                runtime_metrics.emit_audit_event(
                    "flatten_manual_intervention",
                    strategy_id=strategy_id,
                    ticker=item["ticker"],
                    reason=confirmation.get("reason", confirmation["status"]),
                )
            else:
                overall_status = "success"
                runtime_metrics.emit_audit_event(
                    "flatten_confirmed",
                    strategy_id=strategy_id,
                    ticker=item["ticker"],
                    remaining_qty=confirmation.get("remaining_qty", 0),
                )
            results.append(item_result)

        return {
            "status": overall_status,
            "strategy_id": strategy_id,
            "plan": plan,
            "items": results,
        }

    def _flatten_until_target(
        self,
        connector,
        strategy_id: str,
        item: dict,
        timeout_sec: float,
        poll_interval: float,
        max_child_orders: int,
    ) -> dict:
        target_qty = max(int(item["open_qty"]) - int(item["close_qty"]), 0)
        current_qty = int(item["open_qty"])
        attempt = 0
        last_order_id = None

        while current_qty > target_qty and attempt < max_child_orders:
            close_qty = current_qty - target_qty
            validation_item = {**item, "close_qty": close_qty}
            validation = self._validate_broker_position(connector, validation_item)
            if not validation["ok"]:
                return {
                    "status": "manual_intervention_required",
                    "reason": validation["reason"],
                    "remaining_qty": current_qty,
                    "order_id": last_order_id,
                }

            attempt += 1
            close_result = connector.close_position_result(
                account_id=item["account_id"],
                ticker=item["ticker"],
                quantity=close_qty,
                agent_name=strategy_id,
            )
            last_order_id = close_result.transaction_id
            if not last_order_id:
                return {
                    "status": "manual_intervention_required",
                    "reason": close_result.outcome.value,
                    "remaining_qty": current_qty,
                    "order_id": None,
                }

            terminal = self._wait_for_terminal_order(
                connector=connector,
                order_id=last_order_id,
                timeout_sec=timeout_sec,
                poll_interval=poll_interval,
            )
            if terminal["status"] != "terminal":
                return {
                    "status": "manual_intervention_required",
                    "reason": terminal["reason"],
                    "remaining_qty": current_qty,
                    "order_id": last_order_id,
                }

            confirmation = self._wait_for_target(
                    strategy_id=strategy_id,
                    item=item,
                    timeout_sec=timeout_sec,
                    poll_interval=poll_interval,
            )
            current_qty = int(confirmation.get("remaining_qty", current_qty))
            if confirmation["status"] == "success":
                return {
                    "status": "success",
                    "remaining_qty": current_qty,
                    "order_id": last_order_id,
                    "child_orders": attempt,
                }

        return {
            "status": "manual_intervention_required",
            "reason": "flatten_not_confirmed",
            "remaining_qty": current_qty,
            "order_id": last_order_id,
            "child_orders": attempt,
        }

    def _validate_broker_position(self, connector, item: dict) -> dict:
        positions = connector.get_positions(item["account_id"]) or []
        for pos in positions:
            pos_ticker = str(pos.get("ticker", "")).upper()
            pos_board = str(pos.get("board", item["board"])).upper()
            if pos_ticker != item["ticker"] or pos_board != item["board"]:
                continue

            raw_qty = float(pos.get("quantity", 0) or 0)
            if raw_qty == 0:
                break

            broker_side = "buy" if raw_qty > 0 else "sell"
            broker_qty = abs(int(raw_qty))
            if broker_side != item["position_side"]:
                return {"ok": False, "reason": "broker_side_mismatch"}
            if broker_qty < item["close_qty"]:
                return {"ok": False, "reason": "broker_qty_below_strategy_qty"}
            return {"ok": True, "reason": ""}

        return {"ok": False, "reason": "broker_position_not_found"}

    def _wait_for_target(
        self,
        strategy_id: str,
        item: dict,
        timeout_sec: float,
        poll_interval: float,
    ) -> dict:
        target_qty = max(int(item["open_qty"]) - int(item["close_qty"]), 0)
        deadline = time.monotonic() + max(timeout_sec, 0.0)
        while time.monotonic() <= deadline:
            current = self._position_reader(
                strategy_id,
                ticker=item["ticker"],
                board=item["board"],
            )
            current_qty = abs(int(current.get("quantity", 0) or 0))
            if current_qty <= target_qty:
                return {
                    "status": "success",
                    "remaining_qty": current_qty,
                }
            self._sleep(poll_interval)

        current = self._position_reader(
            strategy_id,
            ticker=item["ticker"],
            board=item["board"],
        )
        return {
            "status": "manual_intervention_required",
            "remaining_qty": abs(int(current.get("quantity", 0) or 0)),
            "reason": "flatten_not_confirmed",
        }

    def _wait_for_terminal_order(
        self,
        connector,
        order_id: str,
        timeout_sec: float,
        poll_interval: float,
    ) -> dict:
        deadline = time.monotonic() + max(timeout_sec, 0.0)
        while time.monotonic() <= deadline:
            try:
                info = connector.get_order_status(order_id)
            except Exception as exc:
                logger.warning(
                    f"[StrategyFlatten] get_order_status tid={order_id}: {exc}"
                )
                info = None

            if info:
                status = str(info.get("status", "") or "").lower()
                if status in _TERMINAL_ORDER_STATUSES:
                    return {"status": "terminal", "reason": status}

            self._sleep(poll_interval)

        return {"status": "timeout", "reason": "order_not_terminal"}
