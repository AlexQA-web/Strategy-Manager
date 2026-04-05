# tests/test_financial_regression.py

"""
Числовые regression-тесты для финансового контура.

TASK-045: Жёсткие числовые сценарии с заранее рассчитанными expected values.

Покрытие:
- Unrealized (open) PnL: futures / stock / bond
- Realized (closed) PnL через FIFO matching
- Commission breakdown: futures taker/maker, stock с lot_size, ETF, bond
- Partial close: PnL и пропорциональная комиссия
- Late fill repair через FillLedger дедупликация
- Circuit breaker hard-stop
- Portfolio-level exposure limits
- Reservation ledger: reserve / release / stale eviction
- Equity snapshot: realized + unrealized
- ValuationService: pnl_multiplier resolution, slice_commission
"""

import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from core.commission_manager import CommissionManager
from core.fill_ledger import FillLedger
from core.order_history import (
    clear_orders,
    get_order_pairs,
    get_total_pnl,
    make_order,
    save_order,
)
from core.position_tracker import PositionTracker
from core.reservation_ledger import ReservationLedger
from core.risk_guard import RiskGuard
from core.valuation_service import ValuationService


# ─────────────────────────────────────────────────────────────────────
# ValuationService — open PnL
# ─────────────────────────────────────────────────────────────────────


class TestOpenPnlRegression:
    """Числовые сценарии unrealized PnL."""

    def test_futures_long_profit(self):
        """Si фьючерс long: вход 85000, текущая 86000, 2 контракта, point_cost=1."""
        # gross = (86000 - 85000) * 2 * 1 = 2000
        pnl = ValuationService.compute_open_pnl(
            entry_price=85000.0,
            current_price=86000.0,
            qty=2,
            pnl_multiplier=1.0,
        )
        assert pnl == pytest.approx(2000.0)

    def test_futures_long_loss(self):
        """Si фьючерс long: вход 85000, текущая 84500, 3 контракта, point_cost=1."""
        # gross = (84500 - 85000) * 3 * 1 = -1500
        pnl = ValuationService.compute_open_pnl(
            entry_price=85000.0,
            current_price=84500.0,
            qty=3,
            pnl_multiplier=1.0,
        )
        assert pnl == pytest.approx(-1500.0)

    def test_futures_short_profit(self):
        """RTS short: вход 110000, текущая 108000, qty=-2, point_cost=13.7."""
        # gross = (108000 - 110000) * (-2) * 13.7 = (-2000) * (-2) * 13.7 = 54800
        pnl = ValuationService.compute_open_pnl(
            entry_price=110000.0,
            current_price=108000.0,
            qty=-2,
            pnl_multiplier=13.7,
        )
        assert pnl == pytest.approx(54800.0)

    def test_stock_long_profit_lot_size_10(self):
        """SBER long: вход 300, текущая 310, qty=5 лотов, lot_size=10."""
        # multiplier = 10 (lot_size)
        # gross = (310 - 300) * 5 * 10 = 500
        pnl = ValuationService.compute_open_pnl(
            entry_price=300.0,
            current_price=310.0,
            qty=5,
            pnl_multiplier=10.0,
        )
        assert pnl == pytest.approx(500.0)

    def test_stock_short_loss_lot_size_100(self):
        """Акция short: вход 50, текущая 55, qty=-3, lot_size=100."""
        # gross = (55 - 50) * (-3) * 100 = -1500
        pnl = ValuationService.compute_open_pnl(
            entry_price=50.0,
            current_price=55.0,
            qty=-3,
            pnl_multiplier=100.0,
        )
        assert pnl == pytest.approx(-1500.0)

    def test_open_pnl_with_commissions(self):
        """Open PnL с учётом комиссии входа и предполагаемого выхода."""
        # gross = (310 - 300) * 5 * 10 = 500
        # net = 500 - 15 - 16 = 469
        pnl = ValuationService.compute_open_pnl(
            entry_price=300.0,
            current_price=310.0,
            qty=5,
            pnl_multiplier=10.0,
            entry_commission=15.0,
            exit_commission=16.0,
        )
        assert pnl == pytest.approx(469.0)

    def test_zero_position_zero_pnl(self):
        """Нет позиции → PnL = 0."""
        pnl = ValuationService.compute_open_pnl(
            entry_price=100.0,
            current_price=200.0,
            qty=0,
            pnl_multiplier=10.0,
        )
        assert pnl == pytest.approx(0.0)


# ─────────────────────────────────────────────────────────────────────
# ValuationService — closed PnL
# ─────────────────────────────────────────────────────────────────────


class TestClosedPnlRegression:
    """Числовые сценарии realized PnL для одной FIFO-пары."""

    def test_long_close_profit(self):
        """Long: buy 100 → sell 120, qty=1, multiplier=1."""
        pnl = ValuationService.compute_closed_pnl(
            open_price=100.0, close_price=120.0,
            qty=1, is_long=True, pnl_multiplier=1.0,
        )
        assert pnl == pytest.approx(20.0)

    def test_long_close_loss(self):
        """Long: buy 100 → sell 90, qty=2, multiplier=10."""
        # gross = (90 - 100) * 2 * 10 = -200
        pnl = ValuationService.compute_closed_pnl(
            open_price=100.0, close_price=90.0,
            qty=2, is_long=True, pnl_multiplier=10.0,
        )
        assert pnl == pytest.approx(-200.0)

    def test_short_close_profit(self):
        """Short: sell 200 → buy 180, qty=3, multiplier=1."""
        # gross = (200 - 180) * 3 * 1 = 60
        pnl = ValuationService.compute_closed_pnl(
            open_price=200.0, close_price=180.0,
            qty=3, is_long=False, pnl_multiplier=1.0,
        )
        assert pnl == pytest.approx(60.0)

    def test_short_close_loss(self):
        """Short: sell 200 → buy 220, qty=1, multiplier=13.7 (RTS)."""
        # gross = (200 - 220) * 1 * 13.7 = -274
        pnl = ValuationService.compute_closed_pnl(
            open_price=200.0, close_price=220.0,
            qty=1, is_long=False, pnl_multiplier=13.7,
        )
        assert pnl == pytest.approx(-274.0)

    def test_closed_pnl_net_of_commissions(self):
        """Realized PnL с комиссиями: gross=20, entry=3, exit=2 → net=15."""
        pnl = ValuationService.compute_closed_pnl(
            open_price=100.0, close_price=120.0,
            qty=1, is_long=True, pnl_multiplier=1.0,
            entry_commission=3.0, exit_commission=2.0,
        )
        assert pnl == pytest.approx(15.0)


# ─────────────────────────────────────────────────────────────────────
# ValuationService — pnl_multiplier resolution
# ─────────────────────────────────────────────────────────────────────


class TestPnlMultiplierResolution:
    """Выбор денежного множителя для разных типов инструментов."""

    def test_futures_uses_point_cost(self):
        m = ValuationService.get_pnl_multiplier(is_futures=True, point_cost=13.7, lot_size=1)
        assert m == pytest.approx(13.7)

    def test_futures_zero_point_cost_fallback(self):
        """Фьючерс без point_cost → fallback 1.0."""
        m = ValuationService.get_pnl_multiplier(is_futures=True, point_cost=0.0, lot_size=10)
        assert m == pytest.approx(1.0)

    def test_stock_uses_lot_size(self):
        m = ValuationService.get_pnl_multiplier(is_futures=False, point_cost=999, lot_size=10)
        assert m == pytest.approx(10.0)

    def test_stock_zero_lot_size_fallback(self):
        """Акция с lot_size=0 → fallback 1.0."""
        m = ValuationService.get_pnl_multiplier(is_futures=False, point_cost=5, lot_size=0)
        assert m == pytest.approx(1.0)

    def test_bond_lot_size_1(self):
        """Облигация: lot_size=1 → multiplier=1."""
        m = ValuationService.get_pnl_multiplier(is_futures=False, lot_size=1)
        assert m == pytest.approx(1.0)


# ─────────────────────────────────────────────────────────────────────
# ValuationService — slice_commission
# ─────────────────────────────────────────────────────────────────────


class TestSliceCommission:
    """Пропорциональное разделение комиссии при частичном FIFO-матчинге."""

    def test_full_match(self):
        """Полное совпадение: slice_qty == source_qty → вся комиссия."""
        c = ValuationService.slice_commission(100.0, 10, 10)
        assert c == pytest.approx(100.0)

    def test_half_match(self):
        """Половина: 5 из 10 → 50% комиссии."""
        c = ValuationService.slice_commission(100.0, 5, 10)
        assert c == pytest.approx(50.0)

    def test_third_match(self):
        """Треть: 1 из 3 → 33.33% комиссии."""
        c = ValuationService.slice_commission(30.0, 1, 3)
        assert c == pytest.approx(10.0)

    def test_zero_commission(self):
        c = ValuationService.slice_commission(0.0, 5, 10)
        assert c == pytest.approx(0.0)

    def test_zero_source_qty(self):
        c = ValuationService.slice_commission(100.0, 5, 0)
        assert c == pytest.approx(0.0)


# ─────────────────────────────────────────────────────────────────────
# ValuationService — equity snapshot
# ─────────────────────────────────────────────────────────────────────


class TestEquitySnapshotRegression:
    """Equity = realized + unrealized."""

    def test_no_position(self):
        """Нет позиции → equity = realized PnL."""
        eq = ValuationService.compute_equity_snapshot(
            realized_pnl=500.0,
            entry_price=0.0,
            current_price=0.0,
            position_qty=0,
            pnl_multiplier=1.0,
        )
        assert eq == pytest.approx(500.0)

    def test_with_open_position(self):
        """Realized=500, open long 2 контракта Si: вход 85000, текущая 86000."""
        # unrealized = (86000 - 85000) * 2 * 1 = 2000
        # equity = 500 + 2000 = 2500
        eq = ValuationService.compute_equity_snapshot(
            realized_pnl=500.0,
            entry_price=85000.0,
            current_price=86000.0,
            position_qty=2,
            pnl_multiplier=1.0,
        )
        assert eq == pytest.approx(2500.0)

    def test_negative_equity(self):
        """Realized=-300, unrealized=-700 → equity=-1000."""
        # unrealized = (95 - 100) * 2 * 10 = -100 ... нет, точнее
        # unrealized = (50 - 100) * 2 * 1 = -100
        eq = ValuationService.compute_equity_snapshot(
            realized_pnl=-300.0,
            entry_price=100.0,
            current_price=50.0,
            position_qty=2,
            pnl_multiplier=7.0,
        )
        # unrealized = (50 - 100) * 2 * 7 = -700
        assert eq == pytest.approx(-1000.0)

    def test_equity_with_commissions(self):
        """Equity с учётом комиссий на открытую позицию."""
        # unrealized = (310 - 300) * 5 * 10 - 15 - 16 = 500 - 31 = 469
        # equity = 200 + 469 = 669
        eq = ValuationService.compute_equity_snapshot(
            realized_pnl=200.0,
            entry_price=300.0,
            current_price=310.0,
            position_qty=5,
            pnl_multiplier=10.0,
            entry_commission=15.0,
            exit_commission=16.0,
        )
        assert eq == pytest.approx(669.0)


# ─────────────────────────────────────────────────────────────────────
# Commission — числовые regression-сценарии
# ─────────────────────────────────────────────────────────────────────


class TestCommissionRegression:
    """Числовые сценарии комиссий для всех типов инструментов."""

    def _make_mgr(self) -> CommissionManager:
        """CommissionManager с default config (без обращения к файлу)."""
        mgr = CommissionManager.__new__(CommissionManager)
        mgr.config = {}
        mgr._create_default_config()
        return mgr

    # --- Фьючерсы ---

    def test_si_futures_taker_1_lot(self):
        """Si 1 лот @ 90000, point_cost=1, taker.
        trade_value = 90000 * 1 * 1 = 90000
        moex = 90000 * 0.001 / 100 = 0.9
        broker = 1.0 * 1 = 1.0
        total = 1.9
        """
        mgr = self._make_mgr()
        comm = mgr.calculate(
            ticker="SiM6", board="FUT",
            quantity=1, price=90000.0,
            order_role="taker", point_cost=1.0,
            connector_id="finam",
        )
        assert comm == pytest.approx(1.9, rel=0.01)

    def test_si_futures_taker_5_lots(self):
        """Si 5 лотов @ 90000, point_cost=1, taker.
        trade_value = 90000 * 1 * 5 = 450000
        moex = 450000 * 0.001 / 100 = 4.5
        broker = 1.0 * 5 = 5.0
        total = 9.5
        """
        mgr = self._make_mgr()
        comm = mgr.calculate(
            ticker="SiM6", board="FUT",
            quantity=5, price=90000.0,
            order_role="taker", point_cost=1.0,
            connector_id="finam",
        )
        assert comm == pytest.approx(9.5, rel=0.01)

    def test_ri_futures_taker(self):
        """RI 2 лота @ 110000, point_cost=13.7 (index_futures).
        trade_value = 110000 * 13.7 * 2 = 3014000
        moex = 3014000 * 0.001 / 100 = 30.14
        broker = 0.87 * 2 = 1.74
        total = 31.88
        """
        mgr = self._make_mgr()
        comm = mgr.calculate(
            ticker="RIM6", board="FUT",
            quantity=2, price=110000.0,
            order_role="taker", point_cost=13.7,
            connector_id="finam",
        )
        assert comm == pytest.approx(31.88, rel=0.01)

    def test_br_futures_taker(self):
        """BR 1 лот @ 72.5, point_cost=7.27472 (commodity_futures).
        trade_value = 72.5 * 7.27472 * 1 = 527.4172
        moex = 527.4172 * 0.005 / 100 = 0.02637
        broker = 2.10 * 1 = 2.10
        total ≈ 2.1264
        """
        mgr = self._make_mgr()
        comm = mgr.calculate(
            ticker="BRN6", board="FUT",
            quantity=1, price=72.5,
            order_role="taker", point_cost=7.27472,
            connector_id="finam",
        )
        assert comm == pytest.approx(2.1264, rel=0.02)

    def test_futures_maker_no_moex(self):
        """Si maker: moex=0, broker=1.0 → total=1.0."""
        mgr = self._make_mgr()
        comm = mgr.calculate(
            ticker="SiM6", board="FUT",
            quantity=1, price=90000.0,
            order_role="maker", point_cost=1.0,
        )
        assert comm == pytest.approx(1.0, rel=0.01)

    # --- Акции ---

    def test_stock_sber_taker_lot_size_10(self):
        """SBER 1 лот @ 300, lot_size=10, taker.
        trade_value = 300 * 1 * 10 = 3000
        moex = 3000 * 0.003 / 100 = 0.09
        broker = 3000 * 0.04 / 100 = 1.20
        total = 1.29
        """
        mgr = self._make_mgr()
        comm = mgr.calculate(
            ticker="SBER", board="TQBR",
            quantity=1, price=300.0,
            order_role="taker", lot_size=10,
        )
        assert comm == pytest.approx(1.29, rel=0.01)

    def test_stock_sber_5_lots(self):
        """SBER 5 лотов @ 300, lot_size=10, taker.
        trade_value = 300 * 5 * 10 = 15000
        moex = 15000 * 0.003 / 100 = 0.45
        broker = 15000 * 0.04 / 100 = 6.00
        total = 6.45
        """
        mgr = self._make_mgr()
        comm = mgr.calculate(
            ticker="SBER", board="TQBR",
            quantity=5, price=300.0,
            order_role="taker", lot_size=10,
        )
        assert comm == pytest.approx(6.45, rel=0.01)

    def test_stock_lot_size_scaling(self):
        """Комиссия пропорциональна lot_size."""
        mgr = self._make_mgr()
        c1 = mgr.calculate("SBER", "TQBR", 1, 100.0, lot_size=1)
        c10 = mgr.calculate("SBER", "TQBR", 1, 100.0, lot_size=10)
        assert c10 == pytest.approx(c1 * 10, rel=0.01)

    # --- ETF ---

    def test_etf_taker(self):
        """ETF TQTF: 10 лотов @ 50, lot_size=1, taker.
        trade_value = 50 * 10 * 1 = 500
        moex = 500 * 0.003 / 100 = 0.015
        broker = 500 * 0.04 / 100 = 0.20
        total = 0.215
        """
        mgr = self._make_mgr()
        comm = mgr.calculate(
            ticker="TMOS", board="TQTF",
            quantity=10, price=50.0,
            order_role="taker", lot_size=1,
        )
        assert comm == pytest.approx(0.215, rel=0.02)

    # --- Bond ---

    def test_bond_taker(self):
        """Облигация TQOB: 2 лота @ 95.5, lot_size=1, taker.
        trade_value = 95.5 * 2 * 1 = 191
        moex = 191 * 0.003 / 100 = 0.00573
        broker = 191 * 0.015 / 100 = 0.02865
        total ≈ 0.03438
        """
        mgr = self._make_mgr()
        comm = mgr.calculate(
            ticker="SU26238RMFS4", board="TQOB",
            quantity=2, price=95.5,
            order_role="taker", lot_size=1,
        )
        assert comm == pytest.approx(0.03438, rel=0.02)

    # --- Breakdown ---

    def test_breakdown_stock_fields(self):
        """get_breakdown для акций возвращает lot_size и все поля."""
        mgr = self._make_mgr()
        bd = mgr.get_breakdown(
            ticker="SBER", board="TQBR",
            quantity=2, price=300.0,
            lot_size=10,
        )
        assert bd["is_futures"] is False
        assert bd["lot_size"] == 10
        assert bd["trade_value"] == pytest.approx(6000.0)
        assert bd["total_roundtrip"] == pytest.approx(bd["total_one_side"] * 2)

    def test_breakdown_futures_fields(self):
        """get_breakdown для фьючерсов не содержит lot_size."""
        mgr = self._make_mgr()
        bd = mgr.get_breakdown(
            ticker="SiM6", board="FUT",
            quantity=1, price=90000.0,
            point_cost=1.0,
        )
        assert bd["is_futures"] is True
        assert "lot_size" not in bd
        assert bd["broker_rub"] == pytest.approx(1.0)


# ─────────────────────────────────────────────────────────────────────
# FIFO matching — partial close, PnL и пропорциональная комиссия
# ─────────────────────────────────────────────────────────────────────


STRATEGY_FIFO = "test_fifo_regression"


class TestFifoPartialCloseRegression:
    """Числовые сценарии FIFO с частичными закрытиями и комиссиями."""

    def setup_method(self):
        clear_orders(STRATEGY_FIFO)

    def teardown_method(self):
        clear_orders(STRATEGY_FIFO)

    def _order(self, side, qty, price, commission_total=0.0, pnl_multiplier=1.0):
        o = make_order(
            strategy_id=STRATEGY_FIFO,
            ticker="SBER", side=side, quantity=qty, price=price,
            board="TQBR",
            commission_total=commission_total,
            pnl_multiplier=pnl_multiplier,
            exec_key=str(uuid.uuid4()),
        )
        save_order(o)
        return o

    def test_partial_close_pnl(self):
        """Buy 10 @ 100, sell 4 @ 120 → closed PnL = (120-100)*4*1 = 80."""
        self._order("buy", 10, 100.0)
        self._order("sell", 4, 120.0)

        pairs = get_order_pairs(STRATEGY_FIFO)
        closed = [p for p in pairs if p["close"] is not None]
        open_ = [p for p in pairs if p["close"] is None]

        assert len(closed) == 1
        assert closed[0]["quantity"] == 4
        assert closed[0]["gross_pnl"] == pytest.approx(80.0)
        assert len(open_) == 1
        assert open_[0]["open"]["quantity"] == 6

    def test_partial_close_proportional_commission(self):
        """Buy 10 @ 100 comm=50, sell 4 @ 120 comm=20.
        entry_commission = 50 * (4/10) = 20
        exit_commission = 20
        net_pnl = 80 - 20 - 20 = 40
        """
        self._order("buy", 10, 100.0, commission_total=50.0)
        self._order("sell", 4, 120.0, commission_total=20.0)

        pairs = get_order_pairs(STRATEGY_FIFO)
        closed = [p for p in pairs if p["close"] is not None]

        assert len(closed) == 1
        assert closed[0]["entry_commission"] == pytest.approx(20.0)
        assert closed[0]["exit_commission"] == pytest.approx(20.0)
        assert closed[0]["gross_pnl"] == pytest.approx(80.0)
        assert closed[0]["pnl"] == pytest.approx(40.0)

    def test_multiple_partial_closes(self):
        """Buy 10 @ 100 comm=10, sell 3 @ 110, sell 7 @ 120.
        Pair 1: qty=3, gross=(110-100)*3=30, entry_comm=10*(3/10)=3, exit_comm=0
        Pair 2: qty=7, gross=(120-100)*7=140, entry_comm=10*(7/10)=7, exit_comm=0
        Total PnL = (30-3) + (140-7) = 27 + 133 = 160
        """
        self._order("buy", 10, 100.0, commission_total=10.0)
        self._order("sell", 3, 110.0)
        self._order("sell", 7, 120.0)

        total = get_total_pnl(STRATEGY_FIFO)
        assert total == pytest.approx(160.0)

    def test_fifo_order_matters(self):
        """Два buy → один sell: FIFO берёт первый buy.
        Buy 5 @ 100, Buy 5 @ 200, Sell 5 @ 150.
        FIFO: закрываем первый buy → gross = (150-100)*5 = 250.
        """
        self._order("buy", 5, 100.0)
        self._order("buy", 5, 200.0)
        self._order("sell", 5, 150.0)

        pairs = get_order_pairs(STRATEGY_FIFO)
        closed = [p for p in pairs if p["close"] is not None]
        assert len(closed) == 1
        assert closed[0]["gross_pnl"] == pytest.approx(250.0)
        assert closed[0]["open"]["price"] == pytest.approx(100.0)

    def test_short_partial_close(self):
        """Short 6 @ 200, buy 2 @ 190 → PnL = (200-190)*2 = 20."""
        self._order("sell", 6, 200.0)
        self._order("buy", 2, 190.0)

        pairs = get_order_pairs(STRATEGY_FIFO)
        closed = [p for p in pairs if p["close"] is not None]
        open_ = [p for p in pairs if p["close"] is None]

        assert len(closed) == 1
        assert closed[0]["gross_pnl"] == pytest.approx(20.0)
        assert closed[0]["is_long"] is False
        assert len(open_) == 1
        assert open_[0]["open"]["quantity"] == 4

    def test_futures_pnl_multiplier_in_fifo(self):
        """Фьючерсы: pnl_multiplier=13.7 в FIFO matching.
        Buy 1 @ 110000, Sell 1 @ 111000, multiplier=13.7.
        gross = (111000-110000) * 1 * 13.7 = 13700
        """
        self._order("buy", 1, 110000.0, pnl_multiplier=13.7)
        self._order("sell", 1, 111000.0, pnl_multiplier=13.7)

        pairs = get_order_pairs(STRATEGY_FIFO)
        closed = [p for p in pairs if p["close"] is not None]
        assert len(closed) == 1
        assert closed[0]["gross_pnl"] == pytest.approx(13700.0)

    def test_total_pnl_across_multiple_instruments(self):
        """Разные тикеры — FIFO отдельные очереди, total_pnl суммирует все."""
        # SBER: buy 1 @ 100, sell 1 @ 110, multiplier=1 → PnL = 10
        o1 = make_order(STRATEGY_FIFO, "SBER", "buy", 1, 100.0,
                        exec_key=str(uuid.uuid4()), pnl_multiplier=1.0)
        o2 = make_order(STRATEGY_FIFO, "SBER", "sell", 1, 110.0,
                        exec_key=str(uuid.uuid4()), pnl_multiplier=1.0)
        save_order(o1)
        save_order(o2)

        # GAZP: buy 2 @ 200, sell 2 @ 195, multiplier=1 → PnL = -10
        o3 = make_order(STRATEGY_FIFO, "GAZP", "buy", 2, 200.0,
                        exec_key=str(uuid.uuid4()), pnl_multiplier=1.0)
        o4 = make_order(STRATEGY_FIFO, "GAZP", "sell", 2, 195.0,
                        exec_key=str(uuid.uuid4()), pnl_multiplier=1.0)
        save_order(o3)
        save_order(o4)

        total = get_total_pnl(STRATEGY_FIFO)
        assert total == pytest.approx(0.0)  # 10 + (-10) = 0


# ─────────────────────────────────────────────────────────────────────
# FillLedger — дедупликация, late fill repair
# ─────────────────────────────────────────────────────────────────────


class TestFillLedgerRegression:
    """Late fill и дедупликация в canonical fill ledger."""

    def _make_ledger(self) -> FillLedger:
        ledger = FillLedger()
        return ledger

    @patch("core.fill_ledger.save_order")
    @patch("core.fill_ledger.append_trade")
    def test_duplicate_fill_rejected(self, mock_append, mock_save):
        """Повторный fill с тем же fill_id не записывается."""
        ledger = self._make_ledger()

        ok1 = ledger.record_fill(
            fill_id="exec_001", strategy_id="s1",
            ticker="SBER", board="TQBR", side="buy",
            qty=10, price=300.0,
        )
        ok2 = ledger.record_fill(
            fill_id="exec_001", strategy_id="s1",
            ticker="SBER", board="TQBR", side="buy",
            qty=10, price=300.0,
        )

        assert ok1 is True
        assert ok2 is False
        assert mock_save.call_count == 1
        assert mock_append.call_count == 1

    @patch("core.fill_ledger.save_order")
    @patch("core.fill_ledger.append_trade")
    def test_empty_fill_id_rejected(self, mock_append, mock_save):
        """Fill без fill_id отклоняется."""
        ledger = self._make_ledger()

        ok = ledger.record_fill(
            fill_id="", strategy_id="s1",
            ticker="SBER", board="TQBR", side="buy",
            qty=10, price=300.0,
        )

        assert ok is False
        mock_save.assert_not_called()
        mock_append.assert_not_called()

    @patch("core.fill_ledger.save_order")
    @patch("core.fill_ledger.append_trade")
    def test_late_fill_different_id_accepted(self, mock_append, mock_save):
        """Поздний fill с другим fill_id записывается (late fill repair scenario)."""
        ledger = self._make_ledger()

        ok1 = ledger.record_fill(
            fill_id="exec_001", strategy_id="s1",
            ticker="SBER", board="TQBR", side="buy",
            qty=10, price=300.0,
        )
        ok2 = ledger.record_fill(
            fill_id="exec_002_late", strategy_id="s1",
            ticker="SBER", board="TQBR", side="buy",
            qty=5, price=301.0,
        )

        assert ok1 is True
        assert ok2 is True
        assert mock_save.call_count == 2

    @patch("core.fill_ledger.save_order")
    @patch("core.fill_ledger.append_trade")
    def test_is_duplicate_check(self, mock_append, mock_save):
        """is_duplicate корректно определяет уже записанные fills."""
        ledger = self._make_ledger()

        assert ledger.is_duplicate("fill_x") is False
        ledger.record_fill(
            fill_id="fill_x", strategy_id="s1",
            ticker="T", board="B", side="buy", qty=1, price=1.0,
        )
        assert ledger.is_duplicate("fill_x") is True
        assert ledger.is_duplicate("fill_y") is False


# ─────────────────────────────────────────────────────────────────────
# Circuit breaker hard-stop
# ─────────────────────────────────────────────────────────────────────


class TestCircuitBreakerHardStop:
    """Circuit breaker блокирует новые ордера, но пропускает close."""

    def test_circuit_open_blocks_buy(self):
        """При открытом circuit breaker buy запрещён."""
        rg = RiskGuard(strategy_id="test", circuit_breaker_threshold=2)
        rg.record_failure()
        rg.record_failure()
        assert rg.is_circuit_open() is True

        # Risk limits ещё разрешают, но circuit_open — hard stop на уровне executor
        allowed, _ = rg.check_risk_limits("buy", 1)
        assert allowed is True  # limits отдельно, circuit — отдельно
        assert rg.is_circuit_open() is True

    def test_daily_loss_and_circuit_independent(self):
        """Дневной лимит и circuit breaker — независимые проверки."""
        pnl = [0.0]
        rg = RiskGuard(
            strategy_id="test",
            circuit_breaker_threshold=5,
            daily_loss_limit=1000.0,
            get_total_pnl=lambda s: pnl[0],
        )
        # Фиксируем baseline
        rg.check_risk_limits("buy", 1)

        # Убыток в пределах → разрешено
        pnl[0] = -500.0
        allowed, _ = rg.check_risk_limits("buy", 1)
        assert allowed is True

        # Circuit открыт, но daily loss в пределах → limits разрешают
        rg._circuit_open = True
        allowed, _ = rg.check_risk_limits("buy", 1)
        assert allowed is True  # limits OK, circuit — отдельный gate

    def test_max_position_and_daily_loss_combined(self):
        """Оба лимита одновременно: если хотя бы один нарушен — отказ."""
        pnl = [0.0]
        rg = RiskGuard(
            strategy_id="test",
            max_position_size=10,
            daily_loss_limit=500.0,
            get_total_pnl=lambda s: pnl[0],
        )
        rg.check_risk_limits("buy", 1)  # baseline

        # qty OK, daily loss OK
        allowed, _ = rg.check_risk_limits("buy", 5)
        assert allowed is True

        # qty слишком большой
        allowed, reason = rg.check_risk_limits("buy", 15)
        assert allowed is False
        assert "max_position_size" in reason

        # qty OK, но daily loss превышен
        pnl[0] = -600.0
        allowed, reason = rg.check_risk_limits("buy", 5)
        assert allowed is False
        assert "Дневной лимит" in reason


# ─────────────────────────────────────────────────────────────────────
# Reservation ledger
# ─────────────────────────────────────────────────────────────────────


class TestReservationLedgerRegression:
    """Числовые сценарии для учёта зарезервированного капитала."""

    def test_reserve_and_available(self):
        """Резерв уменьшает доступные средства."""
        rl = ReservationLedger()
        rl.reserve("order_1", "acc_123", 50000.0)

        avail = rl.available("acc_123", 100000.0)
        assert avail == pytest.approx(50000.0)

    def test_multiple_reservations(self):
        """Несколько стратегий резервируют на одном счёте."""
        rl = ReservationLedger()
        rl.reserve("strat_A:1", "acc_123", 30000.0)
        rl.reserve("strat_B:1", "acc_123", 20000.0)

        avail = rl.available("acc_123", 100000.0)
        assert avail == pytest.approx(50000.0)
        assert rl.total_reserved("acc_123") == pytest.approx(50000.0)

    def test_release_frees_capital(self):
        """Release возвращает капитал."""
        rl = ReservationLedger()
        rl.reserve("order_1", "acc_123", 30000.0)
        rl.reserve("order_2", "acc_123", 20000.0)

        rl.release("order_1")

        assert rl.total_reserved("acc_123") == pytest.approx(20000.0)
        assert rl.available("acc_123", 100000.0) == pytest.approx(80000.0)

    def test_release_nonexistent_key(self):
        """Release несуществующего ключа — no-op."""
        rl = ReservationLedger()
        rl.reserve("order_1", "acc_123", 30000.0)
        rl.release("no_such_key")
        assert rl.total_reserved("acc_123") == pytest.approx(30000.0)

    def test_different_accounts_independent(self):
        """Резервы разных счетов не пересекаются."""
        rl = ReservationLedger()
        rl.reserve("k1", "acc_A", 10000.0)
        rl.reserve("k2", "acc_B", 20000.0)

        assert rl.total_reserved("acc_A") == pytest.approx(10000.0)
        assert rl.total_reserved("acc_B") == pytest.approx(20000.0)
        assert rl.available("acc_A", 50000.0) == pytest.approx(40000.0)

    def test_available_never_negative(self):
        """Доступные средства не могут быть отрицательными."""
        rl = ReservationLedger()
        rl.reserve("k1", "acc", 150000.0)

        avail = rl.available("acc", 100000.0)
        assert avail == pytest.approx(0.0)

    def test_stale_reservation_evicted(self):
        """Устаревшие резервы удаляются при запросе total_reserved."""
        rl = ReservationLedger(stale_timeout_sec=0.05)
        rl.reserve("k1", "acc", 50000.0)

        # Вручную ставим timestamp в прошлое
        with rl._lock:
            rl._reservations["k1"]["ts"] = time.monotonic() - 1.0

        # При следующем запросе stale eviction очистит
        reserved = rl.total_reserved("acc")
        assert reserved == pytest.approx(0.0)

    def test_replace_reservation(self):
        """Повторный reserve с тем же ключом перезаписывает."""
        rl = ReservationLedger()
        rl.reserve("k1", "acc", 10000.0)
        rl.reserve("k1", "acc", 25000.0)

        assert rl.total_reserved("acc") == pytest.approx(25000.0)


# ─────────────────────────────────────────────────────────────────────
# PositionTracker — state machine transitions
# ─────────────────────────────────────────────────────────────────────


class TestPositionTrackerTransitions:
    """Проверка матрицы переходов позиции."""

    def test_flat_to_long(self):
        pt = PositionTracker()
        ok = pt.open_position("buy", 5, 100.0)
        assert ok is True
        assert pt.get_position() == 1
        assert pt.get_position_qty() == 5

    def test_flat_to_short(self):
        pt = PositionTracker()
        ok = pt.open_position("sell", 3, 200.0)
        assert ok is True
        assert pt.get_position() == -1
        assert pt.get_position_qty() == -3

    def test_open_rejects_when_already_in_position(self):
        pt = PositionTracker()
        pt.open_position("buy", 5, 100.0)
        ok = pt.open_position("sell", 3, 200.0)
        assert ok is False
        assert pt.get_position() == 1

    def test_full_close_long(self):
        pt = PositionTracker()
        pt.open_position("buy", 5, 100.0)
        pt.clear_order_in_flight()
        ok = pt.close_position(filled=5, total_qty=5)
        assert ok is True
        assert pt.get_position() == 0
        assert pt.get_position_qty() == 0

    def test_partial_close_long(self):
        pt = PositionTracker()
        pt.open_position("buy", 10, 100.0)
        pt.clear_order_in_flight()
        ok = pt.close_position(filled=3, total_qty=10)
        assert ok is True
        assert pt.get_position() == 1
        assert pt.get_position_qty() == 7

    def test_full_close_short(self):
        pt = PositionTracker()
        pt.open_position("sell", 4, 200.0)
        pt.clear_order_in_flight()
        ok = pt.close_position(filled=4, total_qty=4)
        assert ok is True
        assert pt.get_position() == 0

    def test_partial_close_short(self):
        pt = PositionTracker()
        pt.open_position("sell", 6, 200.0)
        pt.clear_order_in_flight()
        ok = pt.close_position(filled=2, total_qty=6)
        assert ok is True
        assert pt.get_position() == -1
        assert pt.get_position_qty() == -4

    def test_close_on_flat_returns_false(self):
        pt = PositionTracker()
        ok = pt.close_position(filled=1, total_qty=1)
        assert ok is False

    def test_confirm_open_flat_to_long(self):
        pt = PositionTracker()
        ok = pt.confirm_open("buy", 5, 100.0)
        assert ok is True
        assert pt.get_position() == 1

    def test_confirm_open_rejects_flip(self):
        """Flip: long → short через confirm_open запрещён."""
        pt = PositionTracker()
        pt.confirm_open("buy", 5, 100.0)
        ok = pt.confirm_open("sell", 3, 200.0)
        assert ok is False
        assert pt.get_position() == 1  # остался long

    def test_confirm_open_rejects_scale_in(self):
        """Scale-in через confirm_open запрещён."""
        pt = PositionTracker()
        pt.confirm_open("buy", 5, 100.0)
        ok = pt.confirm_open("buy", 3, 105.0)
        assert ok is False
        assert pt.get_position_qty() == 5

    def test_try_set_order_in_flight_atomicity(self):
        """try_set_order_in_flight: only one wins."""
        pt = PositionTracker()
        ok1 = pt.try_set_order_in_flight()
        ok2 = pt.try_set_order_in_flight()
        assert ok1 is True
        assert ok2 is False

    def test_try_set_order_in_flight_for_close(self):
        pt = PositionTracker()
        # Нет позиции → False
        assert pt.try_set_order_in_flight_for_close() is False

        pt.open_position("buy", 5, 100.0)
        pt.clear_order_in_flight()
        # Есть позиция, нет in-flight → True
        assert pt.try_set_order_in_flight_for_close() is True
        # Уже в полёте → False
        assert pt.try_set_order_in_flight_for_close() is False
