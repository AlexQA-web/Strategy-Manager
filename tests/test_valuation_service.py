# tests/test_valuation_service.py

"""
Тесты для ValuationService — единого сервиса расчёта PnL, комиссий и equity.
"""

from unittest.mock import MagicMock

import pytest

from core.valuation_service import ValuationService, valuation_service


class TestGetPnlMultiplier:
    """Тесты get_pnl_multiplier."""

    def test_futures_returns_point_cost(self):
        assert valuation_service.get_pnl_multiplier(
            is_futures=True, point_cost=12.5, lot_size=1
        ) == 12.5

    def test_futures_zero_point_cost_fallback(self):
        assert valuation_service.get_pnl_multiplier(
            is_futures=True, point_cost=0.0, lot_size=10
        ) == 1.0

    def test_stock_returns_lot_size(self):
        assert valuation_service.get_pnl_multiplier(
            is_futures=False, point_cost=100.0, lot_size=10
        ) == 10.0

    def test_stock_zero_lot_size_fallback(self):
        assert valuation_service.get_pnl_multiplier(
            is_futures=False, point_cost=100.0, lot_size=0
        ) == 1.0

    def test_stock_negative_lot_size_fallback(self):
        assert valuation_service.get_pnl_multiplier(
            is_futures=False, point_cost=100.0, lot_size=-5
        ) == 1.0


class TestComputeOpenPnl:
    """Тесты compute_open_pnl (unrealized PnL)."""

    def test_long_profit(self):
        result = valuation_service.compute_open_pnl(
            entry_price=100.0,
            current_price=110.0,
            qty=2,
            pnl_multiplier=10.0,
        )
        # (110 - 100) * 2 * 10 = 200
        assert result == 200.0

    def test_long_loss(self):
        result = valuation_service.compute_open_pnl(
            entry_price=100.0,
            current_price=90.0,
            qty=2,
            pnl_multiplier=10.0,
        )
        # (90 - 100) * 2 * 10 = -200
        assert result == -200.0

    def test_short_profit(self):
        result = valuation_service.compute_open_pnl(
            entry_price=100.0,
            current_price=90.0,
            qty=-2,
            pnl_multiplier=10.0,
        )
        # (90 - 100) * (-2) * 10 = 200
        assert result == 200.0

    def test_short_loss(self):
        result = valuation_service.compute_open_pnl(
            entry_price=100.0,
            current_price=110.0,
            qty=-2,
            pnl_multiplier=10.0,
        )
        # (110 - 100) * (-2) * 10 = -200
        assert result == -200.0

    def test_with_commissions(self):
        result = valuation_service.compute_open_pnl(
            entry_price=100.0,
            current_price=110.0,
            qty=1,
            pnl_multiplier=1.0,
            entry_commission=2.0,
            exit_commission=2.0,
        )
        # (110 - 100) * 1 * 1 - 2 - 2 = 6
        assert result == 6.0

    def test_zero_qty_returns_zero(self):
        result = valuation_service.compute_open_pnl(
            entry_price=100.0,
            current_price=110.0,
            qty=0,
            pnl_multiplier=10.0,
        )
        assert result == 0.0


class TestComputeClosedPnl:
    """Тесты compute_closed_pnl (realized PnL для FIFO-пары)."""

    def test_long_profit(self):
        result = valuation_service.compute_closed_pnl(
            open_price=100.0,
            close_price=120.0,
            qty=5,
            is_long=True,
            pnl_multiplier=1.0,
        )
        # (120 - 100) * 5 * 1 = 100
        assert result == 100.0

    def test_long_loss(self):
        result = valuation_service.compute_closed_pnl(
            open_price=120.0,
            close_price=100.0,
            qty=5,
            is_long=True,
            pnl_multiplier=1.0,
        )
        # (100 - 120) * 5 * 1 = -100
        assert result == -100.0

    def test_short_profit(self):
        result = valuation_service.compute_closed_pnl(
            open_price=120.0,
            close_price=100.0,
            qty=5,
            is_long=False,
            pnl_multiplier=1.0,
        )
        # (120 - 100) * 5 * 1 = 100
        assert result == 100.0

    def test_short_loss(self):
        result = valuation_service.compute_closed_pnl(
            open_price=100.0,
            close_price=120.0,
            qty=5,
            is_long=False,
            pnl_multiplier=1.0,
        )
        # (100 - 120) * 5 * 1 = -100
        assert result == -100.0

    def test_with_commissions(self):
        result = valuation_service.compute_closed_pnl(
            open_price=100.0,
            close_price=110.0,
            qty=10,
            is_long=True,
            pnl_multiplier=12.5,
            entry_commission=15.0,
            exit_commission=15.0,
        )
        # (110 - 100) * 10 * 12.5 - 15 - 15 = 1250 - 30 = 1220
        assert result == 1220.0

    def test_futures_multiplier(self):
        # Si: point_cost=1.0, один тик = 1 рубль
        result = valuation_service.compute_closed_pnl(
            open_price=85000.0,
            close_price=85100.0,
            qty=1,
            is_long=True,
            pnl_multiplier=1.0,
        )
        assert result == 100.0

    def test_stock_with_lot_size(self):
        # SBER: lot_size=10, цена 300 → 310
        result = valuation_service.compute_closed_pnl(
            open_price=300.0,
            close_price=310.0,
            qty=1,
            is_long=True,
            pnl_multiplier=10.0,  # lot_size
        )
        # (310 - 300) * 1 * 10 = 100
        assert result == 100.0


class TestComputeCommission:
    """Тесты compute_commission."""

    def test_delegates_to_commission_manager(self):
        mock_cm = MagicMock()
        mock_cm.calculate.return_value = 42.5

        result = valuation_service.compute_commission(
            ticker="SiM5",
            board="SPBFUT",
            qty=10,
            price=85000.0,
            commission_manager=mock_cm,
            point_cost=1.0,
            connector_id="transaq",
            order_role="taker",
        )

        assert result == 42.5
        mock_cm.calculate.assert_called_once_with(
            ticker="SiM5",
            board="SPBFUT",
            quantity=10,
            price=85000.0,
            order_role="taker",
            point_cost=1.0,
            connector_id="transaq",
            lot_size=1,
        )

    def test_none_commission_manager_returns_zero(self):
        result = valuation_service.compute_commission(
            ticker="SiM5",
            board="SPBFUT",
            qty=10,
            price=85000.0,
            commission_manager=None,
        )
        assert result == 0.0

    def test_negative_qty_uses_abs(self):
        mock_cm = MagicMock()
        mock_cm.calculate.return_value = 10.0

        valuation_service.compute_commission(
            ticker="SBER",
            board="TQBR",
            qty=-5,
            price=300.0,
            commission_manager=mock_cm,
        )

        call_args = mock_cm.calculate.call_args
        assert call_args.kwargs["quantity"] == 5


class TestComputeEquitySnapshot:
    """Тесты compute_equity_snapshot."""

    def test_no_position(self):
        result = valuation_service.compute_equity_snapshot(
            realized_pnl=500.0,
            entry_price=0.0,
            current_price=0.0,
            position_qty=0,
            pnl_multiplier=1.0,
        )
        assert result == 500.0

    def test_with_open_position(self):
        result = valuation_service.compute_equity_snapshot(
            realized_pnl=500.0,
            entry_price=100.0,
            current_price=110.0,
            position_qty=2,
            pnl_multiplier=10.0,
            entry_commission=5.0,
            exit_commission=5.0,
        )
        # realized=500, unrealized=(110-100)*2*10 - 5 - 5 = 190
        assert result == 690.0

    def test_negative_unrealized(self):
        result = valuation_service.compute_equity_snapshot(
            realized_pnl=1000.0,
            entry_price=100.0,
            current_price=80.0,
            position_qty=1,
            pnl_multiplier=10.0,
        )
        # realized=1000, unrealized=(80-100)*1*10 = -200
        assert result == 800.0


class TestSliceCommission:
    """Тесты slice_commission."""

    def test_full_slice(self):
        result = valuation_service.slice_commission(100.0, 10, 10)
        assert result == 100.0

    def test_half_slice(self):
        result = valuation_service.slice_commission(100.0, 5, 10)
        assert result == 50.0

    def test_zero_commission(self):
        assert valuation_service.slice_commission(0.0, 5, 10) == 0.0

    def test_zero_slice_qty(self):
        assert valuation_service.slice_commission(100.0, 0, 10) == 0.0

    def test_zero_source_qty(self):
        assert valuation_service.slice_commission(100.0, 5, 0) == 0.0

    def test_negative_commission(self):
        assert valuation_service.slice_commission(-10.0, 5, 10) == 0.0
