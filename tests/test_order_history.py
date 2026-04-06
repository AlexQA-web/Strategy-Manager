# tests/test_order_history.py
import pytest
import core.order_history as order_history_module
from core.order_history import make_order, save_order, get_order_pairs, get_total_pnl, clear_orders


STRATEGY_ID = "test_strategy_fifo"


def setup_function():
    clear_orders(STRATEGY_ID)


def teardown_function():
    clear_orders(STRATEGY_ID)


def _order(side, qty, price, ticker="SBER", board="TQBR"):
    o = make_order(
        strategy_id=STRATEGY_ID,
        ticker=ticker,
        side=side,
        quantity=qty,
        price=price,
        board=board,
        commission=0.0,
        point_cost=1.0,
        exec_key=f"{side}:{qty}:{price}:{ticker}:{id(o if False else object())}",
    )
    # Уникальный exec_key
    import uuid
    o["exec_key"] = str(uuid.uuid4())
    return o


def test_fifo_simple_long():
    """Покупка 1 → продажа 1 → PnL = (sell - buy) * qty"""
    buy = _order("buy", 1, 100.0)
    sell = _order("sell", 1, 120.0)
    save_order(buy)
    save_order(sell)

    pairs = get_order_pairs(STRATEGY_ID)
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair["is_long"] is True
    assert pair["gross_pnl"] == pytest.approx(20.0)
    assert pair["pnl"] == pytest.approx(20.0)  # комиссия = 0


def test_fifo_partial_close():
    """Покупка 3, закрытие 1 → остаток 2 в открытых."""
    buy = _order("buy", 3, 100.0)
    sell = _order("sell", 1, 110.0)
    save_order(buy)
    save_order(sell)

    pairs = get_order_pairs(STRATEGY_ID)
    closed = [p for p in pairs if p["close"] is not None]
    open_ = [p for p in pairs if p["close"] is None]
    assert len(closed) == 1
    assert closed[0]["quantity"] == 1
    assert len(open_) == 1
    assert open_[0]["open"]["quantity"] == 2


def test_short_pnl():
    """Продажа 1 → откуп 1 → PnL = (sell - buy)"""
    sell = _order("sell", 1, 200.0)
    buy = _order("buy", 1, 180.0)
    save_order(sell)
    save_order(buy)

    pairs = get_order_pairs(STRATEGY_ID)
    closed = [p for p in pairs if p["close"] is not None]
    assert len(closed) == 1
    assert closed[0]["gross_pnl"] == pytest.approx(20.0)


def test_commission_reduces_pnl():
    """Комиссия вычитается из gross_pnl."""
    buy = make_order(
        strategy_id=STRATEGY_ID,
        ticker="SBER", side="buy", quantity=1, price=100.0,
        board="TQBR", commission=5.0, commission_total=5.0,
        point_cost=1.0, exec_key="buy-comm-test",
    )
    sell = make_order(
        strategy_id=STRATEGY_ID,
        ticker="SBER", side="sell", quantity=1, price=120.0,
        board="TQBR", commission=5.0, commission_total=5.0,
        point_cost=1.0, exec_key="sell-comm-test",
    )
    save_order(buy)
    save_order(sell)

    pairs = get_order_pairs(STRATEGY_ID)
    closed = [p for p in pairs if p["close"] is not None]
    assert len(closed) == 1
    assert closed[0]["gross_pnl"] == pytest.approx(20.0)
    assert closed[0]["pnl"] == pytest.approx(10.0)  # 20 - 5 - 5


def test_total_pnl():
    """get_total_pnl суммирует все закрытые сделки."""
    for i in range(3):
        buy = make_order(STRATEGY_ID, "SBER", "buy", 1, 100.0, exec_key=f"b{i}")
        sell = make_order(STRATEGY_ID, "SBER", "sell", 1, 110.0, exec_key=f"s{i}")
        save_order(buy)
        save_order(sell)

    total = get_total_pnl(STRATEGY_ID)
    assert total == pytest.approx(30.0)


def test_total_pnl_uses_incremental_accounting_after_save_order(monkeypatch):
    buy = make_order(STRATEGY_ID, "SBER", "buy", 1, 100.0, exec_key="inc-b")
    sell = make_order(STRATEGY_ID, "SBER", "sell", 1, 110.0, exec_key="inc-s")
    save_order(buy)
    save_order(sell)

    monkeypatch.setattr(
        order_history_module,
        "get_order_pairs",
        lambda strategy_id: (_ for _ in ()).throw(AssertionError("full rebuild should not run")),
    )

    total = get_total_pnl(STRATEGY_ID)
    assert total == pytest.approx(10.0)


def test_save_order_returns_duplicate_for_same_exec_key():
    order = make_order(
        strategy_id=STRATEGY_ID,
        ticker="SBER",
        side="buy",
        quantity=1,
        price=100.0,
        exec_key="dup-key",
    )

    assert save_order(order) == "inserted"
    assert save_order(dict(order)) == "duplicate"


def test_save_order_persists_versioned_runtime_json(tmp_path, monkeypatch):
    orders_file = tmp_path / "order_history.json"
    monkeypatch.setattr(order_history_module, "ORDERS_FILE", orders_file)

    order = make_order(
        strategy_id=STRATEGY_ID,
        ticker="SBER",
        side="buy",
        quantity=1,
        price=100.0,
        exec_key="versioned-order",
    )

    assert save_order(order) == "inserted"

    with open(orders_file, "r", encoding="utf-8") as f:
        raw = __import__("json").load(f)

    assert raw["schema_version"] == 1
    assert STRATEGY_ID in raw["payload"]