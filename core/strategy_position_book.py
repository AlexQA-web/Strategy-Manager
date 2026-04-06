"""Strategy-owned position book поверх истории подтверждённых fills."""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from core.order_history import get_order_pairs


def get_strategy_open_lots(
    strategy_id: str,
    ticker: Optional[str] = None,
    board: Optional[str] = None,
) -> list[dict]:
    """Возвращает незакрытые лоты стратегии по FIFO-парам."""
    ticker_filter = str(ticker or "").upper()
    board_filter = str(board or "").upper()

    open_lots: list[dict] = []
    for pair in get_order_pairs(strategy_id):
        if pair.get("close") is not None:
            continue

        order = pair.get("open") or {}
        lot_ticker = str(order.get("ticker", "")).upper()
        lot_board = str(order.get("board", "")).upper()
        if ticker_filter and lot_ticker != ticker_filter:
            continue
        if board_filter and lot_board != board_filter:
            continue

        qty = int(order.get("quantity", 0) or 0)
        side = str(order.get("side", "")).lower()
        if qty <= 0 or side not in {"buy", "sell"}:
            continue

        open_lots.append({
            "strategy_id": strategy_id,
            "ticker": lot_ticker,
            "board": lot_board,
            "side": side,
            "quantity": qty,
            "price": float(order.get("price", 0.0) or 0.0),
            "timestamp": str(order.get("timestamp", "") or ""),
            "commission_total": float(order.get("commission_total", 0.0) or 0.0),
            "order": order,
        })

    open_lots.sort(key=lambda lot: lot["timestamp"])
    return open_lots


def get_strategy_position_book(
    strategy_id: str,
    ticker: Optional[str] = None,
    board: Optional[str] = None,
) -> list[dict]:
    """Строит strategy-owned position book по незакрытым лотам."""
    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for lot in get_strategy_open_lots(strategy_id, ticker=ticker, board=board):
        key = (lot["ticker"], lot["board"], lot["side"])
        grouped[key].append(lot)

    entries: list[dict] = []
    for (entry_ticker, entry_board, entry_side), lots in grouped.items():
        total_qty = sum(int(lot["quantity"]) for lot in lots)
        weighted_price = sum(float(lot["price"]) * int(lot["quantity"]) for lot in lots)
        total_commission = sum(float(lot["commission_total"]) for lot in lots)
        avg_entry_price = weighted_price / total_qty if total_qty > 0 else 0.0
        entries.append({
            "strategy_id": strategy_id,
            "ticker": entry_ticker,
            "board": entry_board,
            "side": entry_side,
            "quantity": total_qty,
            "avg_entry_price": avg_entry_price,
            "entry_commission_total": total_commission,
            "open_lots": list(lots),
        })

    entries.sort(key=lambda entry: (entry["ticker"], entry["board"], entry["side"]))
    return entries


def get_strategy_position(
    strategy_id: str,
    ticker: Optional[str] = None,
    board: Optional[str] = None,
) -> dict:
    """Возвращает одну позицию стратегии или пустой результат."""
    entries = get_strategy_position_book(strategy_id, ticker=ticker, board=board)
    if not entries:
        return {
            "strategy_id": strategy_id,
            "ticker": str(ticker or "").upper(),
            "board": str(board or "").upper(),
            "side": "",
            "quantity": 0,
            "avg_entry_price": 0.0,
            "entry_commission_total": 0.0,
            "open_lots": [],
        }

    if len(entries) > 1:
        raise ValueError(
            f"strategy_id={strategy_id} имеет несколько открытых позиций; "
            f"уточните ticker/board"
        )

    return entries[0]
