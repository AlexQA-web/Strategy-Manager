from collections import deque
import random

import pytest

from core.order_history import clear_orders, get_total_pnl, make_order, save_order
from core.strategy_position_book import get_strategy_position_book
from core.valuation_service import valuation_service


SID = "financial_invariants"


def _save(strategy_id: str, side: str, qty: int, price: float, exec_key: str):
    order = make_order(
        strategy_id=strategy_id,
        ticker="SBER",
        side=side,
        quantity=qty,
        price=price,
        board="TQBR",
        exec_key=exec_key,
    )
    save_order(order)


class TestFinancialInvariants:
    def setup_method(self):
        clear_orders(SID)

    def teardown_method(self):
        clear_orders(SID)

    @pytest.mark.parametrize("seed", [1, 7, 19, 42, 99])
    def test_position_book_and_realized_pnl_match_fifo_sequence(self, seed):
        rng = random.Random(seed)
        open_lots = deque()
        expected_realized = 0.0
        expected_open_qty = 0

        for index in range(25):
            should_buy = expected_open_qty == 0 or rng.random() < 0.6
            qty = rng.randint(1, 3)
            price = round(rng.uniform(90.0, 120.0), 2)

            if should_buy:
                _save(SID, "buy", qty, price, f"buy:{seed}:{index}")
                open_lots.append([qty, price])
                expected_open_qty += qty
                continue

            qty = min(qty, expected_open_qty)
            _save(SID, "sell", qty, price, f"sell:{seed}:{index}")
            expected_open_qty -= qty
            remaining = qty
            while remaining > 0:
                lot_qty, lot_price = open_lots[0]
                matched = min(remaining, lot_qty)
                expected_realized += valuation_service.compute_closed_pnl(
                    open_price=lot_price,
                    close_price=price,
                    qty=matched,
                    is_long=True,
                    pnl_multiplier=1.0,
                )
                remaining -= matched
                lot_qty -= matched
                if lot_qty == 0:
                    open_lots.popleft()
                else:
                    open_lots[0][0] = lot_qty

        position_book = get_strategy_position_book(SID, ticker="SBER", board="TQBR")
        actual_open_qty = sum(int(entry.get("quantity", 0) or 0) for entry in position_book)

        assert actual_open_qty == expected_open_qty
        assert get_total_pnl(SID) == pytest.approx(expected_realized)

    @pytest.mark.parametrize(
        "parts,total",
        [([1, 1, 1], 3), ([2, 3, 5], 10), ([4, 1], 5)],
    )
    def test_commission_slices_preserve_total(self, parts, total):
        total_commission = 137.55
        sliced = [
            valuation_service.slice_commission(total_commission, part, total)
            for part in parts
        ]

        assert sum(sliced) == pytest.approx(total_commission)