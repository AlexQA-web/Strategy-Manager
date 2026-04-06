from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

from core.money import to_decimal, to_storage_float


@dataclass
class InstrumentConstraints:
    min_step: Decimal = Decimal("0.01")
    lot_size: int = 1
    min_qty: int = 1


def build_constraints(sec_info: dict | None = None, lot_size: int = 1) -> InstrumentConstraints:
    sec_info = sec_info or {}
    raw_step = (
        sec_info.get("minstep")
        or sec_info.get("step")
        or sec_info.get("price_step")
        or sec_info.get("min_price_step")
        or 0.01
    )
    raw_lot_size = sec_info.get("lotsize") or sec_info.get("lot_size") or lot_size or 1
    try:
        min_step = to_decimal(raw_step)
    except Exception:
        min_step = Decimal("0.01")
    if min_step <= 0:
        min_step = Decimal("0.01")
    try:
        resolved_lot_size = max(int(raw_lot_size), 1)
    except (TypeError, ValueError):
        resolved_lot_size = 1
    return InstrumentConstraints(min_step=min_step, lot_size=resolved_lot_size, min_qty=1)


def normalize_price(price: float, constraints: InstrumentConstraints) -> float:
    price_decimal = to_decimal(price)
    if price_decimal <= 0:
        return 0.0
    step = constraints.min_step if constraints.min_step > 0 else Decimal("0.01")
    normalized = (price_decimal / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
    return to_storage_float(normalized)


def normalize_quantity(quantity: int, constraints: InstrumentConstraints) -> int:
    qty_decimal = to_decimal(quantity)
    if qty_decimal <= 0:
        return 0
    normalized = qty_decimal.quantize(Decimal("1"), rounding=ROUND_FLOOR)
    return max(int(normalized), constraints.min_qty)


def normalize_notional(price: float, quantity: int, constraints: InstrumentConstraints) -> float:
    normalized_price = to_decimal(normalize_price(price, constraints))
    normalized_qty = Decimal(str(normalize_quantity(quantity, constraints)))
    return to_storage_float(normalized_price * normalized_qty * Decimal(str(constraints.lot_size)))