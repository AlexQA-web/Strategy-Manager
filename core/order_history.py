# core/order_history.py
"""
Хранилище истории ордеров агентов.
Каждый ордер содержит поле commission (руб/лот на одну сторону).
При расчёте PnL пары комиссия вычитается за обе стороны (вход + выход):
    net_pnl = gross_pnl - commission_per_lot * qty * 2
Потребляется: LiveEngine (запись), UI (отображение), equity_tracker (realized PnL).

NOTE: Race condition fix - добавлен _orders_lock для защиты read-modify-write цикла.
"""
import threading
import uuid
from datetime import datetime
from typing import Optional
from loguru import logger

from core.storage import _read, _write, DATA_DIR

# Lock для защиты race condition при одновременной записи ордеров от разных стратегий
_orders_lock = threading.Lock()

ORDERS_FILE = DATA_DIR / "order_history.json"


# ─────────────────────────────────────────────
# Модель ордера
# ─────────────────────────────────────────────

def make_order(
    strategy_id: str,
    ticker: str,
    side: str,           # "buy" | "sell"
    quantity: int,
    price: float,
    board: str = "TQBR",
    comment: str = "",
    commission: float = 0.0,  # комиссия в руб. на 1 лот (одна сторона)
    point_cost: float = 1.0,  # стоимость пункта в рублях
) -> dict:
    return {
        "id":           str(uuid.uuid4()),
        "strategy_id":  strategy_id,
        "ticker":       ticker,
        "board":        board,
        "side":         side,
        "quantity":     quantity,
        "price":        price,
        "timestamp":    datetime.now().isoformat(),
        "status":       "filled",
        "comment":      comment,
        "commission":   commission,  # руб/лот, одна сторона
        "point_cost":   point_cost,  # стоимость пункта в рублях
        "pnl":          None,   # Заполняется при закрытии
        "pair_id":      None,   # ID ордера-открытия (для закрывающих ордеров)
    }


# ─────────────────────────────────────────────
# Хранилище
# ─────────────────────────────────────────────

def _load() -> dict:
    data = _read(ORDERS_FILE)
    return data if isinstance(data, dict) else {}


def _save(data: dict):
    _write(ORDERS_FILE, data)


def save_order(order: dict):
    """Сохраняет ордер в историю. Защищено от race condition через _orders_lock."""
    with _orders_lock:
        data = _load()
        strategy_id = order["strategy_id"]
        if strategy_id not in data:
            data[strategy_id] = []
        data[strategy_id].append(order)
        _save(data)
    logger.debug(
        f"[{strategy_id}] Ордер сохранён: "
        f"{order['side'].upper()} {order['ticker']} "
        f"x{order['quantity']} @ {order['price']}"
    )


def get_orders(strategy_id: str) -> list[dict]:
    """Возвращает историю ордеров стратегии, сортированную по времени."""
    with _orders_lock:
        data = _load()
        orders = data.get(strategy_id, [])
        return sorted(orders, key=lambda o: o["timestamp"])


def update_order_pnl(order_id: str, strategy_id: str, pnl: float):
    """Обновляет П/У закрывающего ордера. Защищено от race condition."""
    with _orders_lock:
        data = _load()
        for order in data.get(strategy_id, []):
            if order["id"] == order_id:
                order["pnl"] = pnl
                break
        _save(data)


def clear_orders(strategy_id: str):
    """Очищает историю ордеров стратегии. Защищено от race condition."""
    with _orders_lock:
        data = _load()
        data.pop(strategy_id, None)
        _save(data)


# ─────────────────────────────────────────────
# Сопоставление пар ордеров (открытие → закрытие)
# ─────────────────────────────────────────────

def get_order_pairs(strategy_id: str) -> list[dict]:
    """
    Сопоставляет ордера в пары (открытие + закрытие) по FIFO.
    Возвращает список пар с рассчитанным П/У.

    Пара:
    {
        "open":   {...ордер открытия...},
        "close":  {...ордер закрытия...} или None (позиция ещё открыта),
        "pnl":    float или None,
        "is_long": bool,
    }
    """
    orders = get_orders(strategy_id)
    pairs = []
    # FIFO очередь незакрытых ордеров (индекс 0 - самый старый)
    open_queue: list[dict] = []

    for order in orders:
        side = order["side"]
        qty  = order["quantity"]
        remaining_qty = qty

        while remaining_qty > 0 and open_queue:
            open_order = open_queue[0]  # Берём самый старый ордер (FIFO)
            open_qty = open_order["quantity"]

            if side == open_order["side"]:
                # Та же сторона — пирамидинг/усреднение
                break  # Выходим, новый ордер становится в очередь

            # Противоположная сторона — закрытие позиции
            close_qty = min(open_qty, remaining_qty)
            is_long = open_order["side"] == "buy"
            open_price = open_order["price"]
            close_price = order["price"]
            
            # Учитываем point_cost для фьючерсов
            point_cost = open_order.get("point_cost", 1.0)

            if is_long:
                gross_pnl = (close_price - open_price) * close_qty * point_cost
            else:
                gross_pnl = (open_price - close_price) * close_qty * point_cost

            # Комиссия: берём максимум из open/close ордера
            commission_per_lot = max(
                float(open_order.get("commission", 0.0)),
                float(order.get("commission", 0.0)),
            )
            total_commission = commission_per_lot * close_qty * 2  # вход + выход
            net_pnl = gross_pnl - total_commission

            pairs.append({
                "open": open_order,
                "close": order,
                "gross_pnl": round(gross_pnl, 4),
                "commission": round(total_commission, 4),
                "pnl": round(net_pnl, 4),
                "is_long": is_long,
            })

            # Обновляем остатки
            remaining_qty -= close_qty
            if close_qty >= open_qty:
                # Открывающий ордер полностью закрыт
                open_queue.pop(0)
            else:
                # Частичное закрытие — обновляем количество
                open_queue[0] = {
                    **open_order,
                    "quantity": open_qty - close_qty,
                }

        # Если остаток не закрыт — добавляем в очередь
        if remaining_qty > 0:
            if remaining_qty < qty:
                # Частично закрыт другими ордерами — создаём новую запись с остатком
                open_queue.append({
                    **order,
                    "quantity": remaining_qty,
                })
            else:
                # Не был закрыт вообще — добавляем как есть
                open_queue.append(order)

    # Незакрытые позиции
    for open_order in open_queue:
        pairs.append({
            "open": open_order,
            "close": None,
            "pnl": None,
            "is_long": open_order["side"] == "buy",
        })

    return pairs
def get_total_pnl(strategy_id: str) -> Optional[float]:
    """Нарастающий итог П/У по всем закрытым сделкам стратегии."""
    pairs = get_order_pairs(strategy_id)
    closed = [p["pnl"] for p in pairs if p["pnl"] is not None]
    if not closed:
        return None
    return round(sum(closed), 2)


def get_pnl_by_ticker(strategy_id: str) -> dict[str, Optional[float]]:
    """П/У по каждому тикеру стратегии (только закрытые сделки).

    Возвращает: {ticker: pnl_float | None}
    None — если по тикеру нет закрытых сделок.
    """
    pairs = get_order_pairs(strategy_id)
    result: dict[str, list[float]] = {}
    for p in pairs:
        if p["pnl"] is None:
            continue
        ticker = p["open"]["ticker"]
        result.setdefault(ticker, []).append(p["pnl"])
    return {t: round(sum(v), 2) for t, v in result.items()}
