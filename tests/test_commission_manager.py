# tests/test_commission_manager.py
import pytest
from core.commission_manager import CommissionManager


def test_futures_taker_commission():
    """Валютный фьючерс: taker комиссия = moex_part + broker_part."""
    mgr = CommissionManager()
    comm = mgr.calculate(
        ticker="SiM6", board="FUT",
        quantity=1, price=90000.0,
        order_role="taker", point_cost=1.0,
        connector_id="finam",
    )
    # moex_pct = 0.001% от (90000 * 1 * 1) = 0.9
    # broker = 1.0 руб/контракт
    # итого ≈ 1.9
    assert comm == pytest.approx(1.9, rel=0.01)


def test_maker_commission_no_moex():
    """Для мейкера moex_part = 0."""
    mgr = CommissionManager()
    comm_taker = mgr.calculate(
        ticker="SiM6", board="FUT",
        quantity=1, price=90000.0,
        order_role="taker", point_cost=1.0,
    )
    comm_maker = mgr.calculate(
        ticker="SiM6", board="FUT",
        quantity=1, price=90000.0,
        order_role="maker", point_cost=1.0,
    )
    # maker должен быть меньше taker (нет moex части)
    assert comm_maker < comm_taker


def test_stock_commission_uses_lot_size():
    """Для акций комиссия масштабируется с lot_size."""
    mgr = CommissionManager()
    comm_lot1 = mgr.calculate(
        ticker="SBER", board="TQBR",
        quantity=1, price=100.0,
        order_role="taker", lot_size=1,
    )
    comm_lot10 = mgr.calculate(
        ticker="SBER", board="TQBR",
        quantity=1, price=100.0,
        order_role="taker", lot_size=10,
    )
    assert comm_lot10 == pytest.approx(comm_lot1 * 10, rel=0.01)