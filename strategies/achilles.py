# strategies/achilles.py
"""
Achilles — Mean Reversion на корзине инструментов.

Логика:
  time_snapshot  — запоминаем last price всех инструментов
  time_signal    — считаем % изменение, покупаем топ-рост, продаём топ-падение
  time_close     — плановое закрытие лимитками
  time_emergency — аварийное закрытие рыночными ордерами

Каждый инструмент: allow_buy, allow_sell.
order_mode: limit_book (bid/ask ± offset) | limit_price (last price).
"""

from loguru import logger

# ── Состояние (сбрасывается в on_start и каждый день) ────────────────────────
# NOTE: Добавлен threading.Lock для защиты от race condition при одновременном
# доступе из poll-loop и chase-потоков
import threading
_state_lock = threading.Lock()

_reference_prices: dict[str, float] = {}
_positions: dict[str, dict] = {}   # {ticker: {side, qty, board, status: "opening"|"open"|"closing"}}
_pending_orders: dict[str, dict] = {}  # {ticker: {side, qty, board, status, mode, is_close}}
_snapshot_done: bool = False
_signal_done: bool = False
_close_done: bool = False


def reset_state():
    """Сбрасывает состояние стратегии. Вызывать при каждом перезапуске LiveEngine."""
    global _reference_prices, _positions, _pending_orders, _snapshot_done, _signal_done, _close_done
    with _state_lock:
        _reference_prices = {}
        _positions = {}
        _pending_orders = {}
        _snapshot_done = False
        _signal_done = False
        _close_done = False


def get_info() -> dict:
    return {
        "name":        "Achilles",
        "version":     "1.0",
        "author":      "Alexey",
        "description": (
            "Mean Reversion на корзине инструментов. "
            "Снимок цен → сигнал по % изменению → закрытие по времени."
        ),
    }


def get_params() -> dict:
    return {
        "time_snapshot": {
            "type":        "time",
            "default":     600,
            "label":       "Время снимка",
            "description": "Время фиксации референсных цен",
        },
        "time_signal": {
            "type":        "time",
            "default":     720,
            "label":       "Время сигнала",
            "description": "Время входа в позиции",
        },
        "time_close": {
            "type":        "time",
            "default":     960,
            "label":       "Время закрытия",
            "description": "Плановое закрытие лимитками",
        },
        "time_emergency": {
            "type":        "time",
            "default":     1110,
            "label":       "Аварийное закрытие",
            "description": "Закрытие рыночными ордерами если позиции не закрыты",
        },
        "spread_offset": {
            "type":        "float",
            "default":     2.0,
            "min":         0.0,
            "max":         100.0,
            "label":       "Отступ от стакана",
            "description": "Отступ от bid/ask при лимитных заявках (Лимитка Стакан)",
        },
        "long_percent": {
            "type":        "float",
            "default":     1.0,
            "min":         0.0,
            "max":         100.0,
            "label":       "Процент_Лонг",
            "description": "Минимальный рост лидера корзины для входа в лонг, в %",
        },
        "short_percent": {
            "type":        "float",
            "default":     -1.0,
            "min":         -100.0,
            "max":         0.0,
            "label":       "Процент_Шорт",
            "description": "Максимальное падение лидера снижения для входа в шорт, в %",
        },
        "order_mode": {
            "type":        "select",
            "default":     "limit_book",
            "options":     ["market", "limit_book", "limit_price"],
            "labels":      ["Рыночная", "Лимитка (Стакан)", "Лимитка (Цена)"],
            "label":       "Тип заявки",
            "description": "market — рыночная; limit_book — bid/ask ± offset с автоперестановкой; limit_price — last price до 23:45",
        },
        "qty": {
            "type":        "int",
            "default":     1,
            "min":         1,
            "max":         1000,
            "label":       "Лотность",
            "description": "Количество лотов на каждый инструмент",
        },
        "instruments": {
            "type":    "instruments",
            "default": [
                {"ticker": "SiH6", "board": "SPBFUT", "allow_buy": True,  "allow_sell": True},
                {"ticker": "SBER", "board": "TQBR",   "allow_buy": True,  "allow_sell": False},
                {"ticker": "GAZP", "board": "TQBR",   "allow_buy": True,  "allow_sell": True},
                {"ticker": "LKOH", "board": "TQBR",   "allow_buy": True,  "allow_sell": True},
                {"ticker": "ROSN", "board": "TQBR",   "allow_buy": True,  "allow_sell": True},
                {"ticker": "YNDX", "board": "TQBR",   "allow_buy": True,  "allow_sell": True},
                {"ticker": "VTBR", "board": "TQBR",   "allow_buy": False, "allow_sell": True},
            ],
            "label":       "Инструменты",
            "description": "Корзина инструментов с разрешениями на покупку/продажу",
        },
    }


def on_start(params: dict, connector) -> None:
    reset_state()  # Сбрасывает все глобальные переменные
    logger.info("[Achilles] Запуск")


def on_stop(params: dict, connector) -> None:
    logger.info("[Achilles] Остановка")


def on_tick(tick_data: dict, params: dict, connector) -> None:
    pass


# ── on_bar — только логика времени, без исполнения ───────────────────────────

def on_bar(bars: list[dict], position: int, params: dict) -> dict:
    global _snapshot_done, _signal_done, _close_done

    if not bars:
        return {"action": None}

    current  = bars[-1]
    time_min = current["time_min"]
    weekday  = current["weekday"]

    if weekday in (6, 7):
        return {"action": None}

    time_snapshot  = int(params.get("time_snapshot",  600))
    time_signal    = int(params.get("time_signal",    720))
    time_close     = int(params.get("time_close",     960))
    time_emergency = int(params.get("time_emergency", 1110))

    with _state_lock:
        # Сброс флагов в начале нового дня
        if time_min < time_snapshot:
            _snapshot_done = False
            _signal_done   = False
            _close_done    = False

        if time_min >= time_snapshot and not _snapshot_done:
            _snapshot_done = True
            return {"action": "snapshot"}

        if time_min >= time_signal and not _signal_done and _snapshot_done:
            _signal_done = True
            return {"action": "signal"}

        has_open_positions = any(pos.get("status", "open") == "open" for pos in _positions.values())

        if time_min >= time_close and not _close_done and has_open_positions:
            return {"action": "close_limit"}

        if time_min >= time_emergency and has_open_positions:
            return {"action": "close_market"}

    return {"action": None}


# ── execute_signal — вызывается LiveEngine вместо _execute_signal ─────────────

def execute_signal(signal: dict, connector, params: dict, account_id: str):
    global _close_done
    action = signal.get("action")
    if action == "snapshot":
        _do_snapshot(connector, params)
    elif action == "signal":
        _do_signal(connector, params, account_id)
    elif action == "close_limit":
        _do_close_limit(connector, params, account_id)
        with _state_lock:
            _close_done = True
    elif action == "close_market":
        _do_close_market(connector, params, account_id)


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _get_instruments(params: dict) -> list[dict]:
    return params.get("instruments") or get_params()["instruments"]["default"]


def _get_price(connector, board: str, ticker: str) -> float:
    """Получает last price через get_best_quote или get_last_price."""
    try:
        if hasattr(connector, "get_best_quote"):
            q = connector.get_best_quote(board, ticker)
            if q:
                return q.get("last") or q.get("bid") or q.get("offer") or 0.0
        if hasattr(connector, "get_last_price"):
            return connector.get_last_price(ticker, board) or 0.0
    except Exception as e:
        logger.warning(f"[Achilles] get_price {ticker}: {e}")
    return 0.0


def _calc_qty(connector, account_id: str, board: str, ticker: str,
              side: str, params: dict) -> int:
    """Рассчитывает динамический лот по формуле:
        floor(свободные_средства / (текущая_цена * кол-во_бумаг_в_1_лоте))

    Если свободные средства или цена недоступны — возвращаем qty из params.
    """
    qty = int(params.get("qty", 1))

    try:
        free_money = connector.get_free_money(account_id) if hasattr(connector, "get_free_money") else None
        if not free_money or free_money <= 0:
            return qty

        price = _get_price(connector, board, ticker)
        if not price or price <= 0:
            return qty

        sec_info = {}
        if hasattr(connector, "get_sec_info"):
            sec_info = connector.get_sec_info(ticker, board) or {}
        lot_size = int(sec_info.get("lotsize") or sec_info.get("lot_size") or 1)

        dyn_qty = int(free_money / (price * lot_size))
        logger.debug(
            f"[Achilles] _calc_qty {ticker}: free={free_money:.2f} "
            f"price={price:.4f} lot_size={lot_size} → qty={dyn_qty}"
        )
        return max(dyn_qty, 1)
    except Exception as e:
        logger.warning(f"[Achilles] _calc_qty {ticker}: {e}")
        return qty


def _record_trade(
    ticker: str,
    board: str,
    side: str,
    qty: int,
    price: float,
    order_role: str,
    strategy_id: str,
    connector_id: str,
    comment: str = "",
    order_ref: str = "",
):
    """Записывает исполненную сделку в order_history.json и trades_history.json.

    Зеркалит логику LiveEngine._record_trade():
      - комиссия рассчитывается через commission_manager.calculate() (авто-режим)
      - point_cost=1.0 (Ахиллес торгует только акции TQBR)
    """
    if qty <= 0 or price <= 0:
        return
    point_cost = 1.0
    try:
        from core.commission_manager import commission_manager
        commission_rub = commission_manager.calculate(
            ticker=ticker,
            board=board,
            quantity=qty,
            price=price,
            order_role=order_role,
            point_cost=point_cost,
            connector_id=connector_id or "transaq",
        )
    except Exception as e:
        logger.warning(f"[Achilles] _record_trade комиссия {ticker}: {e}")
        commission_rub = 0.0

    commission_per_lot = commission_rub / qty if qty > 0 else commission_rub

    logger.info(
        f"[Achilles] Запись сделки: {side.upper()} {ticker}x{qty} @{price:.4f} "
        f"role={order_role} комиссия={commission_rub:.4f} руб "
        f"({commission_per_lot:.4f} руб/лот)"
    )

    try:
        from core.order_history import make_order, save_order
        order = make_order(
            strategy_id=strategy_id,
            ticker=ticker,
            side=side,
            quantity=qty,
            price=price,
            board=board,
            comment=comment,
            commission=commission_per_lot,
            commission_total=commission_rub,
            point_cost=point_cost,
            exec_key=(
                f"achilles:{strategy_id}:{ticker}:{order_role}:{order_ref}:{side}:{qty}:{round(float(price), 8)}"
                if order_ref else ""
            ),
            source="achilles_execute_signal",
        )
        save_order(order)
    except Exception as e:
        logger.warning(f"[Achilles] _record_trade (order_history) {ticker}: {e}")

    try:
        from datetime import datetime as _dt_now
        from core.storage import append_trade
        trade = {
            "strategy_id": strategy_id,
            "agent_name":  "Achilles",
            "ticker":      ticker,
            "board":       board,
            "side":        side,
            "qty":         qty,
            "price":       price,
            "commission":  commission_rub,
            "order_type":  "market" if order_role == "taker" else "limit",
            "comment":     comment,
            "dt":          _dt_now.now().isoformat(),
        }
        append_trade(trade)
    except Exception as e:
        logger.warning(f"[Achilles] _record_trade (storage) {ticker}: {e}")


def _place(connector, account_id: str, board: str, ticker: str,
           side: str, qty: int, order_mode: str,
           spread_offset: float, agent_name: str = "Achilles",
           strategy_id: str = "", connector_id: str = "",
           is_close: bool = False) -> bool:
    """
    Выставляет заявку в зависимости от order_mode:
      - "market"      — рыночная заявка
      - "limit_book"  — лимитка по bid/offer ± spread_offset (ChaseOrder с автоперестановкой)
      - "limit_price" — лимитка по last price; мониторится до исполнения или 23:45
    """
    try:
        if order_mode == "market":
            tid = connector.place_order(
                account_id=account_id,
                ticker=ticker,
                side=side,
                quantity=qty,
                order_type="market",
                board=board,
                agent_name=agent_name,
            )
            if tid:
                logger.info(f"[Achilles] MARKET {side.upper()} {ticker}x{qty} tid={tid}")
                price = _get_price(connector, board, ticker)
                if strategy_id:
                    _record_trade(
                        ticker=ticker, board=board, side=side, qty=qty,
                        price=price, order_role="taker",
                        strategy_id=strategy_id, connector_id=connector_id,
                        comment=f"market tid={tid}",
                        order_ref=str(tid),
                    )
                return True
            else:
                logger.error(
                    f"[Achilles] ОШИБКА заявки: агент=Achilles тикер={ticker} "
                    f"сторона={side.upper()} qty={qty} цена=market "
                    f"вид=market — ордер не выставлен"
                )
                return False

        elif order_mode == "limit_book":
            # ChaseOrder: лимитка по bid/offer с автоперестановкой в фоновом потоке
            import threading
            from core.chase_order import ChaseOrder

            with _state_lock:
                pending = _pending_orders.get(ticker)
                if pending is not None:
                    logger.warning(
                        f"[Achilles] {ticker}: уже есть активный лимитный ордер "
                        f"status={pending.get('status')} mode={pending.get('mode')}, пропуск"
                    )
                    return False

                if is_close:
                    current = _positions.get(ticker)
                    if current is None:
                        logger.warning(f"[Achilles] CLOSE LIMIT {ticker} пропущен: позиции нет")
                        return False
                    if current.get("status") == "closing":
                        logger.warning(f"[Achilles] CLOSE LIMIT {ticker} пропущен: уже закрывается")
                        return False
                    current["status"] = "closing"
                else:
                    if ticker in _positions:
                        logger.warning(f"[Achilles] {ticker}: позиция уже существует, повторный вход запрещён")
                        return False
                    _positions[ticker] = {
                        "side": side,
                        "qty": int(qty),
                        "board": board,
                        "status": "opening",
                    }

                _pending_orders[ticker] = {
                    "side": side,
                    "qty": int(qty),
                    "board": board,
                    "status": "closing" if is_close else "opening",
                    "mode": order_mode,
                    "is_close": is_close,
                }

            def _run_chase():
                global _positions, _pending_orders
                chase = ChaseOrder(
                    connector=connector,
                    account_id=account_id,
                    ticker=ticker,
                    side=side,
                    quantity=qty,
                    board=board,
                    agent_name=agent_name,
                )
                chase.wait(timeout=60)
                if not chase.is_done:
                    chase.cancel()
                if chase.filled_qty == 0:
                    logger.error(
                        f"[Achilles] ОШИБКА заявки: агент=Achilles тикер={ticker} "
                        f"сторона={side.upper()} qty={qty} цена=bid/offer "
                        f"вид=limit_book(стакан) — ничего не исполнено за 60 сек"
                    )
                    with _state_lock:
                        _pending_orders.pop(ticker, None)
                        if is_close:
                            current = _positions.get(ticker)
                            if current is not None and current.get("status") == "closing":
                                current["status"] = "open"
                        else:
                            current = _positions.get(ticker)
                            if current is not None and current.get("status") == "opening":
                                _positions.pop(ticker, None)
                else:
                    logger.info(
                        f"[Achilles] Chase {side.upper()} {ticker}x{qty} "
                        f"filled={chase.filled_qty} avg={chase.avg_price:.4f}"
                    )
                    if strategy_id:
                        _record_trade(
                            ticker=ticker, board=board, side=side,
                            qty=chase.filled_qty, price=chase.avg_price,
                            order_role="maker",
                            strategy_id=strategy_id, connector_id=connector_id,
                            comment=f"limit_book chase avg={chase.avg_price:.4f}",
                        )
                    with _state_lock:
                        _pending_orders.pop(ticker, None)
                        if is_close:
                            current = _positions.get(ticker)
                            if current is not None:
                                remaining_qty = max(int(current.get("qty", qty)) - int(chase.filled_qty), 0)
                                if remaining_qty > 0:
                                    current["qty"] = remaining_qty
                                    current["status"] = "open"
                                else:
                                    _positions.pop(ticker, None)
                        else:
                            current = _positions.get(ticker)
                            if current is None:
                                _positions[ticker] = {
                                    "side": side,
                                    "qty": int(chase.filled_qty),
                                    "board": board,
                                    "status": "open",
                                }
                            else:
                                current["side"] = side
                                current["qty"] = int(chase.filled_qty)
                                current["board"] = board
                                current["status"] = "open"

            t = threading.Thread(target=_run_chase, daemon=True,
                                 name=f"achilles-chase-{ticker}-{side}")
            t.start()
            return True

        else:  # limit_price
            # Лимитная по last price — висит до исполнения или до 23:45
            import threading, time as _time
            from datetime import datetime as _dt

            with _state_lock:
                pending = _pending_orders.get(ticker)
                if pending is not None:
                    logger.warning(
                        f"[Achilles] {ticker}: уже есть активный лимитный ордер "
                        f"status={pending.get('status')} mode={pending.get('mode')}, пропуск"
                    )
                    return False

                if is_close:
                    current = _positions.get(ticker)
                    if current is None:
                        logger.warning(f"[Achilles] CLOSE LIMIT {ticker} пропущен: позиции нет")
                        return False
                    if current.get("status") == "closing":
                        logger.warning(f"[Achilles] CLOSE LIMIT {ticker} пропущен: уже закрывается")
                        return False
                    current["status"] = "closing"
                else:
                    if ticker in _positions:
                        logger.warning(f"[Achilles] {ticker}: позиция уже существует, повторный вход запрещён")
                        return False
                    _positions[ticker] = {
                        "side": side,
                        "qty": int(qty),
                        "board": board,
                        "status": "opening",
                    }

            price = _get_price(connector, board, ticker)
            if not price:
                logger.warning(f"[Achilles] limit_price: нет цены для {ticker}, пропуск")
                with _state_lock:
                    if is_close:
                        current = _positions.get(ticker)
                        if current is not None and current.get("status") == "closing":
                            current["status"] = "open"
                    else:
                        current = _positions.get(ticker)
                        if current is not None and current.get("status") == "opening":
                            _positions.pop(ticker, None)
                return False

            tid = connector.place_order(
                account_id=account_id,
                ticker=ticker,
                side=side,
                quantity=qty,
                order_type="limit",
                price=round(price, 6),
                board=board,
                agent_name=agent_name,
            )
            if not tid:
                logger.error(
                    f"[Achilles] ОШИБКА заявки: агент=Achilles тикер={ticker} "
                    f"сторона={side.upper()} qty={qty} цена={price:.4f} "
                    f"вид=limit_price — ордер не выставлен"
                )
                with _state_lock:
                    if is_close:
                        current = _positions.get(ticker)
                        if current is not None and current.get("status") == "closing":
                            current["status"] = "open"
                    else:
                        current = _positions.get(ticker)
                        if current is not None and current.get("status") == "opening":
                            _positions.pop(ticker, None)
                return False

            with _state_lock:
                _pending_orders[ticker] = {
                    "side": side,
                    "qty": int(qty),
                    "board": board,
                    "status": "closing" if is_close else "opening",
                    "mode": order_mode,
                    "is_close": is_close,
                    "tid": tid,
                }

            logger.info(f"[Achilles] LIMIT {side.upper()} {ticker}x{qty} @{price:.4f} tid={tid}")

            from config.settings import TRADING_END_TIME_MIN
            _TERMINAL = {"matched", "cancelled", "canceled", "denied", "removed", "expired", "killed"}
            CANCEL_MIN = TRADING_END_TIME_MIN

            def _monitor():
                global _positions, _pending_orders
                filled = 0
                while True:
                    try:
                        info = connector.get_order_status(tid)
                        if info:
                            status = info.get("status", "")
                            b = info.get("balance")
                            q = info.get("quantity")
                            if b is not None and q is not None:
                                filled = int(q) - int(b)
                            if status in _TERMINAL:
                                logger.info(f"[Achilles] LIMIT tid={tid} {ticker} {status} filled={filled}/{qty}")
                                break
                    except Exception as e:
                        logger.warning(f"[Achilles] monitor tid={tid} {ticker}: {e}")

                    now_min = _dt.now().hour * 60 + _dt.now().minute
                    if now_min >= CANCEL_MIN:
                        logger.info(f"[Achilles] LIMIT tid={tid} {ticker} снимается в 23:45 (filled={filled}/{qty})")
                        try:
                            connector.cancel_order(tid, account_id)
                        except Exception:
                            pass
                        deadline = _time.monotonic() + 2.0
                        while _time.monotonic() < deadline:
                            _time.sleep(0.1)
                            try:
                                info2 = connector.get_order_status(tid)
                                if info2 and info2.get("status", "") in _TERMINAL:
                                    b2 = info2.get("balance")
                                    q2 = info2.get("quantity")
                                    if b2 is not None and q2 is not None:
                                        filled = int(q2) - int(b2)
                                    break
                            except Exception:
                                pass
                        break
                    _time.sleep(1.0)

                if filled > 0 and strategy_id:
                    _record_trade(
                        ticker=ticker, board=board, side=side,
                        qty=filled, price=price,
                        order_role="maker",
                        strategy_id=strategy_id, connector_id=connector_id,
                        comment=f"limit_price tid={tid} filled={filled}/{qty}",
                        order_ref=str(tid),
                    )
                with _state_lock:
                    _pending_orders.pop(ticker, None)
                    if is_close:
                        current = _positions.get(ticker)
                        if current is not None:
                            remaining_qty = max(int(current.get("qty", qty)) - int(filled), 0)
                            if remaining_qty > 0:
                                current["qty"] = remaining_qty
                                current["status"] = "open"
                            else:
                                _positions.pop(ticker, None)
                    else:
                        if filled > 0:
                            current = _positions.get(ticker)
                            if current is None:
                                _positions[ticker] = {
                                    "side": side,
                                    "qty": int(filled),
                                    "board": board,
                                    "status": "open",
                                }
                            else:
                                current["side"] = side
                                current["qty"] = int(filled)
                                current["board"] = board
                                current["status"] = "open"
                        else:
                            current = _positions.get(ticker)
                            if current is not None and current.get("status") == "opening":
                                _positions.pop(ticker, None)

            t = threading.Thread(target=_monitor, daemon=True,
                                 name=f"achilles-lp-{ticker}-{tid}")
            t.start()
            return True

    except Exception as e:
        logger.error(f"[Achilles] _place {ticker}: {e}")
        return False


def _do_snapshot(connector, params: dict):
    """Фиксируем референсные цены."""
    global _reference_prices
    instruments = _get_instruments(params)
    new_prices = {}
    for instr in instruments:
        ticker = instr["ticker"]
        board  = instr.get("board", "TQBR")
        price  = _get_price(connector, board, ticker)
        if price:
            new_prices[ticker] = price
            logger.info(f"[Achilles] Снимок {ticker}: {price}")
        else:
            logger.warning(f"[Achilles] Снимок {ticker}: нет цены")
    with _state_lock:
        _reference_prices = new_prices


def _do_signal(connector, params: dict, account_id: str):
    """Считаем % изменение, входим в топ-рост и топ-падение."""
    global _positions
    with _state_lock:
        if not _reference_prices:
            logger.warning("[Achilles] Нет референсных цен, сигнал пропущен")
            return
        ref_copy = dict(_reference_prices)

    instruments    = _get_instruments(params)
    qty            = int(params.get("qty", 1))
    order_mode     = params.get("order_mode", "limit_book")
    spread_offset  = float(params.get("spread_offset", 2.0))
    long_percent   = float(params.get("long_percent", 1.0))
    short_percent  = float(params.get("short_percent", -1.0))
    strategy_id    = params.get("_strategy_id", "")
    connector_id   = params.get("_connector_id", "")

    # Считаем % изменение
    changes: dict[str, float] = {}
    for instr in instruments:
        ticker = instr["ticker"]
        ref    = ref_copy.get(ticker)
        if not ref:
            continue
        cur = _get_price(connector, instr.get("board", "TQBR"), ticker)
        if cur:
            changes[ticker] = (cur - ref) / ref * 100.0

    if not changes:
        logger.warning("[Achilles] Нет данных для расчёта изменений")
        return

    logger.info(f"[Achilles] Изменения: { {k: f'{v:.2f}%' for k, v in changes.items()} }")

    # Топ роста / падения берём по всей корзине, вход — только если выполнен порог
    instr_map = {i["ticker"]: i for i in instruments}
    sorted_changes = sorted(changes.items(), key=lambda x: x[1], reverse=True)

    top_long = next(
        ((ticker, pct, instr_map.get(ticker, {}))
         for ticker, pct in sorted_changes
         if instr_map.get(ticker, {}).get("allow_buy", True)),
        None,
    )
    if top_long is not None:
        ticker, pct, instr = top_long
        if pct > long_percent:
            board = instr.get("board", "TQBR")
            actual_qty = _calc_qty(connector, account_id, board, ticker, "buy", params)
            ok = _place(connector, account_id, board, ticker, "buy", actual_qty,
                        order_mode, spread_offset,
                        strategy_id=strategy_id, connector_id=connector_id)
            if ok:
                if order_mode == "market":
                    with _state_lock:
                        _positions[ticker] = {"side": "buy", "qty": actual_qty, "board": board, "status": "open"}
                logger.info(
                    f"[Achilles] BUY {ticker} (+{pct:.2f}%) qty={actual_qty} "
                    f"порог={long_percent:.2f}%"
                )
        else:
            logger.info(
                f"[Achilles] Лонг пропущен: лидер {ticker} (+{pct:.2f}%) "
                f"не превысил порог {long_percent:.2f}%"
            )
    else:
        logger.info("[Achilles] Лонг пропущен: нет инструментов с allow_buy=true")

    top_short = next(
        ((ticker, pct, instr_map.get(ticker, {}))
         for ticker, pct in reversed(sorted_changes)
         if instr_map.get(ticker, {}).get("allow_sell", True)),
        None,
    )
    if top_short is not None:
        ticker, pct, instr = top_short
        if pct < short_percent:
            board = instr.get("board", "TQBR")
            actual_qty = _calc_qty(connector, account_id, board, ticker, "sell", params)
            ok = _place(connector, account_id, board, ticker, "sell", actual_qty,
                        order_mode, spread_offset,
                        strategy_id=strategy_id, connector_id=connector_id)
            if ok:
                if order_mode == "market":
                    with _state_lock:
                        _positions[ticker] = {"side": "sell", "qty": actual_qty, "board": board, "status": "open"}
                logger.info(
                    f"[Achilles] SELL {ticker} ({pct:.2f}%) qty={actual_qty} "
                    f"порог={short_percent:.2f}%"
                )
        else:
            logger.info(
                f"[Achilles] Шорт пропущен: лидер {ticker} ({pct:.2f}%) "
                f"не достиг порога {short_percent:.2f}%"
            )
    else:
        logger.info("[Achilles] Шорт пропущен: нет инструментов с allow_sell=true")


def _do_close_limit(connector, params: dict, account_id: str):
    """Плановое закрытие лимитками."""
    global _positions
    order_mode    = params.get("order_mode", "limit_book")
    spread_offset = float(params.get("spread_offset", 2.0))
    strategy_id   = params.get("_strategy_id", "")
    connector_id  = params.get("_connector_id", "")

    with _state_lock:
        positions_snapshot = list(_positions.items())

    for ticker, pos in positions_snapshot:
        if pos.get("status", "open") != "open":
            continue
        board      = pos.get("board", "TQBR")
        qty        = pos["qty"]
        close_side = "sell" if pos["side"] == "buy" else "buy"
        ok = _place(connector, account_id, board, ticker, close_side, qty,
                    order_mode, spread_offset,
                    strategy_id=strategy_id, connector_id=connector_id,
                    is_close=True)
        if ok:
            logger.info(f"[Achilles] CLOSE LIMIT {ticker}")
            with _state_lock:
                if ticker in _positions and order_mode == "market":
                    _positions.pop(ticker, None)


def _do_close_market(connector, params: dict, account_id: str):
    """Аварийное закрытие рыночными ордерами."""
    global _positions
    strategy_id  = params.get("_strategy_id", "")
    connector_id = params.get("_connector_id", "")
    # Закрываем ВСЕ позиции, включая те что уже в статусе "closing"
    with _state_lock:
        positions_snapshot = list(_positions.items())

    for ticker, pos in positions_snapshot:
        board      = pos.get("board", "TQBR")
        qty        = pos["qty"]
        close_side = "sell" if pos["side"] == "buy" else "buy"
        try:
            tid = connector.place_order(
                account_id=account_id,
                ticker=ticker,
                side=close_side,
                quantity=qty,
                order_type="market",
                board=board,
                agent_name="Achilles",
            )
            if tid:
                logger.info(f"[Achilles] CLOSE MARKET {ticker} tid={tid}")
                if strategy_id:
                    price = _get_price(connector, board, ticker)
                    _record_trade(
                        ticker=ticker, board=board, side=close_side, qty=qty,
                        price=price, order_role="taker",
                        strategy_id=strategy_id, connector_id=connector_id,
                        comment=f"close_market tid={tid}",
                        order_ref=str(tid),
                    )
                with _state_lock:
                    _pending_orders.pop(ticker, None)
                    _positions.pop(ticker, None)
            else:
                logger.error(f"[Achilles] CLOSE MARKET {ticker} — не удалось")
        except Exception as e:
            logger.error(f"[Achilles] CLOSE MARKET {ticker}: {e}")
