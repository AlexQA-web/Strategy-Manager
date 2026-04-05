# core/order_history.py
"""
Хранилище истории ордеров агентов.

Каждый ордер хранит:
- commission: руб/лот на одну сторону (legacy, для обратной совместимости)
- commission_total: абсолютная комиссия в рублях за всю исполненную сторону

При расчёте PnL пары используется точная абсолютная комиссия:
    net_pnl = gross_pnl - entry_commission_abs - exit_commission_abs

Потребляется: LiveEngine (запись), UI (отображение), equity_tracker (realized PnL).

NOTE: Race condition fix — используется единый _rwlock из storage.py для защиты
read-modify-write цикла, что предотвращает потерю данных при параллельных save_order.
"""
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from loguru import logger

from core.moex_api import MOEXClient
from core.storage import read_json, DATA_DIR, _rwlock, _read as _storage_read, _write_unsafe_inner as _storage_write_unlocked
from core.valuation_service import valuation_service

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
    commission_total: float | None = None,  # абсолютная комиссия за всю сторону
    exec_key: str = "",
    source: str = "",
    pnl_multiplier: float = 0.0,  # денежный множитель для PnL (0 = вычислять автоматически)
) -> Dict[str, Any]:
    qty_abs = abs(int(quantity or 0))
    commission_per_lot = float(commission or 0.0)
    if commission_total is None:
        commission_total = commission_per_lot * qty_abs
    else:
        commission_total = float(commission_total)

    order = {
        "id":                 str(uuid.uuid4()),
        "strategy_id":        strategy_id,
        "ticker":             ticker,
        "board":              board,
        "side":               side,
        "quantity":           quantity,
        "price":              price,
        "timestamp":          datetime.now().isoformat(),
        "status":             "filled",
        "comment":            comment,
        "commission":         commission_per_lot,  # руб/лот, одна сторона (legacy)
        "commission_total":   commission_total,    # руб за всю сторону
        "point_cost":         point_cost,          # стоимость пункта в рублях
        "pnl":                None,   # Заполняется при закрытии
        "pair_id":            None,   # ID ордера-открытия (для закрывающих ордеров)
        "exec_key":           str(exec_key or ""),
        "source":             str(source or ""),
    }
    if pnl_multiplier > 0:
        order["pnl_multiplier"] = pnl_multiplier
    return order


# ─────────────────────────────────────────────
# Хранилище
# ─────────────────────────────────────────────

def _load(use_cache: bool = True) -> Dict[str, Any]:
    """Читает order_history.json.

    При use_cache=False читает с диска напрямую (без read_lock) —
    безопасно вызывать внутри write_lock, не создавая deadlock.
    """
    data = _storage_read(ORDERS_FILE, use_cache=use_cache)
    return data if isinstance(data, dict) else {}


def _save(data: Dict[str, Any]) -> None:
    """Пишет order_history.json без захвата lock.

    Вызывать только внутри _rwlock.write_lock() — иначе не потокобезопасно.
    """
    _storage_write_unlocked(ORDERS_FILE, data)


# Ключ FIFO: (strategy_id, ticker, board)
FIFO_KEY_FIELDS = ("strategy_id", "ticker", "board")


def _key(order: Dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(order.get("strategy_id", "")),
        str(order.get("ticker", "")).upper(),
        str(order.get("board", "")).upper(),
    )


def save_order(order: Dict[str, Any]) -> None:
    """Сохраняет ордер в историю. Защищено от race condition через _rwlock.write_lock()."""
    strategy_id = order["strategy_id"]
    exec_key = str(order.get("exec_key", "") or "")
    if not exec_key:
        logger.warning(f"[{strategy_id}] save_order: нет exec_key, запись пропущена")
        return

    with _rwlock.write_lock():
        data = _load(use_cache=False)
        if strategy_id not in data:
            data[strategy_id] = []
        for existing in data[strategy_id]:
            if str(existing.get("exec_key", "") or "") == exec_key:
                logger.debug(f"[{strategy_id}] duplicate execution ignored: exec_key={exec_key}")
                return
        # Логирование chase-ордеров для отладки
        if exec_key.startswith("chase:"):
            logger.info(
                f"[{strategy_id}] Запись chase-ордера: exec_key={exec_key}, "
                f"side={order['side']}, qty={order['quantity']}, price={order['price']}"
            )
        data[strategy_id].append(order)
        _save(data)
    logger.debug(
        f"[{strategy_id}] Ордер сохранён: "
        f"{order['side'].upper()} {order['ticker']} "
        f"x{order['quantity']} @ {order['price']}"
    )


def get_orders(strategy_id: str) -> List[Dict[str, Any]]:
    """Возвращает историю ордеров стратегии, сортированную по времени."""
    data = _load()
    orders = data.get(strategy_id, [])
    return sorted(orders, key=lambda o: o["timestamp"])


def update_order_pnl(order_id: str, strategy_id: str, pnl: float) -> None:
    """Обновляет П/У закрывающего ордера. Защищено от race condition."""
    with _rwlock.write_lock():
        data = _load(use_cache=False)
        for order in data.get(strategy_id, []):
            if order["id"] == order_id:
                order["pnl"] = pnl
                break
        _save(data)


def clear_orders(strategy_id: str) -> None:
    """Очищает историю ордеров стратегии. Защищено от race condition."""
    with _rwlock.write_lock():
        data = _load(use_cache=False)
        data.pop(strategy_id, None)
        _save(data)

# ─────────────────────────────────────────────
# Утилиты комиссии
# ─────────────────────────────────────────────


def get_order_commission_total(order: Dict[str, Any]) -> float:
    """Возвращает абсолютную комиссию ордера в рублях за всю сторону."""
    if "commission_total" in order:
        try:
            return float(order.get("commission_total", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
    try:
        per_lot = float(order.get("commission", 0.0) or 0.0)
    except (TypeError, ValueError):
        per_lot = 0.0
    qty_abs = abs(int(order.get("quantity", 0) or 0))
    return per_lot * qty_abs


def get_order_pnl_multiplier(order: Dict[str, Any]) -> float:
    """Возвращает денежный множитель для расчёта PnL по ордеру.

    Приоритет:
    1) Явный pnl_multiplier в ордере.
    2) MOEX API по типу инструмента: фьючерсы — point_cost, акции — lot_size,
       облигации — facevalue * minstep (если minstep нет, используем 0.01).
    3) point_cost из ордера.
    4) Fallback 1.0
    """
    try:
        explicit = float(order.get('pnl_multiplier', 0.0) or 0.0)
        if explicit > 0:
            return explicit
    except (TypeError, ValueError):
        pass

    board = str(order.get('board', '') or '').upper()
    ticker = str(order.get('ticker', '') or '').strip().upper()

    def _is_futures(b: str) -> bool:
        return 'FUT' in b or b == 'OPT'

    def _is_bond(b: str) -> bool:
        return b.startswith('TQO') or b.startswith('TQCB') or b in {'TQOD', 'TQOB', 'TQCB', 'TQCBP', 'TQOBP'}

    if ticker:
        try:
            if _is_futures(board):
                info = MOEXClient.get_instrument_info(ticker, sec_type='futures')
                if info and float(info.get('point_cost') or 0.0) > 0:
                    return float(info['point_cost'])
            elif _is_bond(board):
                # Облигации есть в stock API MOEX
                # Котировка в % от номинала: pnl_multiplier = facevalue * minstep / 100
                info = MOEXClient.get_instrument_info(ticker, sec_type='stock')
                if info:
                    facevalue = float(info.get('facevalue') or 0.0)
                    minstep = float(info.get('minstep') or 0.0)
                    if facevalue > 0:
                        step = minstep if minstep > 0 else 0.01
                        return facevalue * step / 100
            else:
                info = MOEXClient.get_instrument_info(ticker, sec_type='stock')
                if info and int(info.get('lot_size') or 0) > 0:
                    return float(info['lot_size'])
        except Exception:
            pass

    try:
        point_cost = float(order.get('point_cost', 0.0) or 0.0)
        if point_cost > 0:
            return point_cost
    except (TypeError, ValueError):
        pass

    return 1.0


def get_total_commission(strategy_id: str) -> float:
    """Возвращает накопленную общую комиссию по агенту."""
    orders = get_orders(strategy_id)
    total = sum(get_order_commission_total(order) for order in orders)
    return round(total, 2)


def get_open_commission(strategy_id: str) -> Optional[float]:
    """Возвращает комиссию текущей открытой позиции по агенту.

    Это фактически уже уплаченная комиссия по незакрытым остаткам FIFO.
    Если открытой позиции нет — возвращает None.
    """
    pairs = get_order_pairs(strategy_id)
    open_pairs = [p for p in pairs if p.get("close") is None]
    if not open_pairs:
        return None
    total = sum(get_order_commission_total(p["open"]) for p in open_pairs)
    return round(total, 2)


# ─────────────────────────────────────────────
# Сопоставление пар ордеров (открытие → закрытие)
# ─────────────────────────────────────────────


def get_order_pairs(strategy_id: str) -> List[Dict[str, Any]]:
    """
    Сопоставляет ордера в пары (открытие + закрытие) по FIFO.
    Возвращает список пар с рассчитанным П/У.

    Очереди ведутся отдельно по ключу (strategy_id, ticker, board),
    чтобы исключить матчинг разных инструментов.

    Пара:
    {
        "open":   {...ордер открытия...},
        "close":  {...ордер закрытия...} или None (позиция ещё открыта),
        "pnl":    float или None,
        "is_long": bool,
    }

    NOTE: Защищено от race condition через _orders_lock.
    """
    def _slice_commission(total_commission: float, slice_qty: int, source_qty: int) -> float:
        return valuation_service.slice_commission(total_commission, slice_qty, source_qty)

    # Защищаем всю обработку lock-ом для избежания race condition
    with _rwlock.read_lock():
        data = _storage_read(ORDERS_FILE, use_cache=True)
        if not isinstance(data, dict):
            data = {}
        orders = data.get(strategy_id, [])
        orders = sorted(orders, key=lambda o: o["timestamp"])

        pairs: list[dict] = []
        open_queues: dict[tuple[str, str, str], list[dict]] = {}

        for order in orders:
            key = _key(order)
            queue = open_queues.setdefault(key, [])

            side = order["side"]
            qty = int(order["quantity"])
            remaining_qty = qty
            remaining_commission_total = get_order_commission_total(order)

            while remaining_qty > 0 and queue:
                open_order = queue[0]  # Берём самый старый ордер (FIFO) в своей паре
                open_qty = int(open_order["quantity"])

                if side == open_order["side"]:
                    # Та же сторона — пирамидинг/усреднение
                    break  # Выходим, новый ордер становится в очередь

                # Противоположная сторона — закрытие позиции
                close_qty = min(open_qty, remaining_qty)
                is_long = open_order["side"] == "buy"
                open_price = open_order["price"]
                close_price = order["price"]

                # Денежный множитель: для фьючерсов point_cost, для акций lot_size
                pnl_multiplier = get_order_pnl_multiplier(open_order)

                open_commission_total = get_order_commission_total(open_order)
                entry_commission = _slice_commission(open_commission_total, close_qty, open_qty)
                exit_commission = _slice_commission(remaining_commission_total, close_qty, remaining_qty)
                total_commission = entry_commission + exit_commission

                gross_pnl = valuation_service.compute_closed_pnl(
                    open_price=open_price,
                    close_price=close_price,
                    qty=close_qty,
                    is_long=is_long,
                    pnl_multiplier=pnl_multiplier,
                    entry_commission=0.0,
                    exit_commission=0.0,
                )
                net_pnl = gross_pnl - total_commission

                open_pair_order = {
                    **open_order,
                    "quantity": close_qty,
                    "commission_total": entry_commission,
                    "commission": entry_commission / close_qty if close_qty > 0 else 0.0,
                }
                close_pair_order = {
                    **order,
                    "quantity": close_qty,
                    "commission_total": exit_commission,
                    "commission": exit_commission / close_qty if close_qty > 0 else 0.0,
                }

                pairs.append({
                    "open": open_pair_order,
                    "close": close_pair_order,
                    "quantity": close_qty,
                    "gross_pnl": round(gross_pnl, 4),
                    "commission": round(total_commission, 4),
                    "entry_commission": round(entry_commission, 4),
                    "exit_commission": round(exit_commission, 4),
                    "pnl": round(net_pnl, 4),
                    "is_long": is_long,
                })

                # Обновляем остатки
                remaining_qty -= close_qty
                remaining_commission_total -= exit_commission
                if close_qty >= open_qty:
                    # Открывающий ордер полностью закрыт
                    queue.pop(0)
                else:
                    # Частичное закрытие — обновляем количество и остаток комиссии
                    open_remaining_qty = open_qty - close_qty
                    open_remaining_commission = open_commission_total - entry_commission
                    queue[0] = {
                        **open_order,
                        "quantity": open_remaining_qty,
                        "commission_total": open_remaining_commission,
                        "commission": (
                            open_remaining_commission / open_remaining_qty
                            if open_remaining_qty > 0 else 0.0
                        ),
                    }

            # Если остаток не закрыт — добавляем в очередь своей пары
            if remaining_qty > 0:
                if remaining_qty < qty:
                    # Частично закрыт другими ордерами — создаём новую запись с остатком
                    queue.append({
                        **order,
                        "quantity": remaining_qty,
                        "commission_total": remaining_commission_total,
                        "commission": (
                            remaining_commission_total / remaining_qty
                            if remaining_qty > 0 else 0.0
                        ),
                    })
                else:
                    # Не был закрыт вообще — добавляем как есть
                    if "commission_total" not in order:
                        order = {
                            **order,
                            "commission_total": remaining_commission_total,
                        }
                    queue.append(order)

        # Незакрытые позиции по всем ключам
        for queue in open_queues.values():
            for open_order in queue:
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



def get_closed_order_pairs(strategy_id: str, ticker: Optional[str] = None) -> List[Dict[str, Any]]:
    """Возвращает только закрытые пары сделок, опционально по тикеру."""
    pairs = [p for p in get_order_pairs(strategy_id) if p.get("close") is not None]
    if ticker:
        ticker_upper = ticker.upper()
        pairs = [p for p in pairs if str(p.get("open", {}).get("ticker", "")).upper() == ticker_upper]
    return pairs

