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

# Явная adapter-декларация: стратегия использует зарегистрированный
# custom execution adapter для управления корзиной инструментов.
__execution_adapter__ = "achilles-basket"

# ── Контекст стратегии (инкапсулирует всё mutable state) ─────────────────────
import threading


class AchillesContext:
    """Контекст одного экземпляра стратегии Achilles.

    Инкапсулирует всё mutable state и собственный lock.
    Создаётся через get_context() при первом обращении.
    """

    _registry: dict[str, "AchillesContext"] = {}
    _registry_lock = threading.Lock()

    def __init__(self):
        self.lock = threading.Lock()
        self.reference_prices: dict[str, float] = {}
        self.positions: dict[str, dict] = {}
        self.pending_orders: dict[str, dict] = {}
        self.snapshot_done: bool = False
        self.signal_done: bool = False
        self.close_done: bool = False

    def reset(self):
        with self.lock:
            self.reference_prices.clear()
            self.positions.clear()
            self.pending_orders.clear()
            self.snapshot_done = False
            self.signal_done = False
            self.close_done = False

    @classmethod
    def get(cls, strategy_id: str) -> "AchillesContext":
        """Возвращает контекст для strategy_id, создаёт при первом обращении."""
        with cls._registry_lock:
            if strategy_id not in cls._registry:
                cls._registry[strategy_id] = AchillesContext()
            return cls._registry[strategy_id]

    @classmethod
    def remove(cls, strategy_id: str):
        with cls._registry_lock:
            cls._registry.pop(strategy_id, None)

    @classmethod
    def clear_all(cls):
        with cls._registry_lock:
            cls._registry.clear()


def _get_state(strategy_id: str) -> AchillesContext:
    """Возвращает контекст конкретного агента."""
    return AchillesContext.get(strategy_id)


def reset_state(strategy_id: str = ""):
    """Сбрасывает состояние конкретного агента."""
    if strategy_id:
        ctx = AchillesContext.get(strategy_id)
        ctx.reset()
    else:
        AchillesContext.clear_all()


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
    strategy_id = params.get("_strategy_id", "default")
    reset_state(strategy_id)    # Сбрасываем только своё состояние
    logger.info(f"[Achilles:{strategy_id}] Запуск")


def on_stop(params: dict, connector) -> None:
    logger.info("[Achilles] Остановка")


def on_tick(tick_data: dict, params: dict, connector) -> None:
    pass


# ── on_bar — только логика времени, без исполнения ───────────────────────────

def on_bar(bars: list[dict], position: int, params: dict) -> dict:
    strategy_id = params.get("_strategy_id", "default")
    ctx = _get_state(strategy_id)

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

    with ctx.lock:
        # Сброс флагов в начале нового дня
        if time_min < time_snapshot:
            ctx.snapshot_done = False
            ctx.signal_done   = False
            ctx.close_done    = False

        if time_min >= time_snapshot and not ctx.snapshot_done:
            ctx.snapshot_done = True
            return {"action": "snapshot"}

        if time_min >= time_signal and not ctx.signal_done and ctx.snapshot_done:
            ctx.signal_done = True
            return {"action": "signal"}

        has_open_positions = any(pos.get("status", "open") == "open" for pos in ctx.positions.values())

        if time_min >= time_close and not ctx.close_done and has_open_positions:
            return {"action": "close_limit"}

        if time_min >= time_emergency and has_open_positions:
            return {"action": "close_market"}

    return {"action": None}


# ── execute_signal — вызывается LiveEngine вместо _execute_signal ─────────────

def execute_signal(signal: dict, connector, params: dict, account_id: str):
    strategy_id = params.get("_strategy_id", "default")
    ctx = _get_state(strategy_id)
    action = signal.get("action")
    if action == "snapshot":
        _do_snapshot(connector, params, strategy_id)
    elif action == "signal":
        _do_signal(connector, params, account_id, strategy_id)
    elif action == "close_limit":
        _do_close_limit(connector, params, account_id, strategy_id)
        with ctx.lock:
            ctx.close_done = True
    elif action == "close_market":
        _do_close_market(connector, params, account_id, strategy_id)


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


def _record_fill(
    ticker: str,
    board: str,
    side: str,
    qty: int,
    price: float,
    order_role: str,
    strategy_id: str,
    connector_id: str,
    comment: str = "",
    fill_id: str = "",
):
    """Записывает исполненную сделку через canonical FillLedger.

    Комиссия рассчитывается через commission_manager.calculate() (авто-режим).
    """
    if qty <= 0 or price <= 0:
        return
    point_cost = 1.0
    commission_rub = 0.0
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
        logger.warning(f"[Achilles] _record_fill комиссия {ticker}: {e}")

    commission_per_lot = commission_rub / qty if qty > 0 else commission_rub

    if not fill_id:
        import time as _time_mod
        fill_id = f"achilles:{strategy_id}:{ticker}:{side}:{int(_time_mod.time() * 1000)}"

    try:
        from core.fill_ledger import fill_ledger
        from core.valuation_service import valuation_service

        pnl_mult = valuation_service.get_pnl_multiplier(
            is_futures=(board == "SPBFUT"),
            point_cost=point_cost,
            lot_size=1,
        )

        fill_ledger.record_fill(
            fill_id=fill_id,
            strategy_id=strategy_id,
            ticker=ticker,
            board=board,
            side=side,
            qty=qty,
            price=price,
            agent_name="Achilles",
            comment=comment,
            order_type="market" if order_role == "taker" else "limit",
            commission_per_lot=commission_per_lot,
            commission_total=commission_rub,
            point_cost=point_cost,
            pnl_multiplier=pnl_mult,
            source="achilles",
        )
    except Exception as e:
        logger.error(f"[Achilles] _record_fill {ticker}: {e}")


def _place(connector, account_id: str, board: str, ticker: str,
           side: str, qty: int, order_mode: str,
           spread_offset: float, agent_name: str = "Achilles",
           strategy_id: str = "", connector_id: str = "",
        is_close: bool = False, params: dict | None = None) -> bool:
    """Выставляет заявку через OrderPlacer с управлением состоянием."""
    from core.order_placer import OrderPlacer

    ctx = _get_state(strategy_id) if strategy_id else None
    placer = OrderPlacer(connector, agent_name=agent_name)
    params = params or {}

    # ── Проверка дубликатов ордеров (специфично для Achilles) ──
    if order_mode in ('limit_book', 'limit_price'):
        if ctx is not None:
            with ctx.lock:
                pending = ctx.pending_orders.get(ticker)
                if pending is not None:
                    logger.warning(
                        f"[Achilles] {ticker}: уже есть активный лимитный ордер "
                        f"status={pending.get('status')} mode={pending.get('mode')}, пропуск"
                    )
                    return False

                if is_close:
                    current = ctx.positions.get(ticker)
                    if current is None:
                        logger.warning(f"[Achilles] CLOSE LIMIT {ticker} пропущен: позиции нет")
                        return False
                    if current.get("status") == "closing":
                        logger.warning(f"[Achilles] CLOSE LIMIT {ticker} пропущен: уже закрывается")
                        return False
                    current["status"] = "closing"
                else:
                    if ticker in ctx.positions:
                        logger.warning(f"[Achilles] {ticker}: позиция уже существует, повторный вход запрещён")
                        return False
                    ctx.positions[ticker] = {
                        "side": side,
                        "qty": int(qty),
                        "board": board,
                        "status": "opening",
                    }

                ctx.pending_orders[ticker] = {
                    "side": side,
                    "qty": int(qty),
                    "board": board,
                    "status": "closing" if is_close else "opening",
                    "mode": order_mode,
                    "is_close": is_close,
                }

    reservation_key = ""
    if not is_close:
        current_price = _get_price(connector, board, ticker)

        risk_check = params.get("_pretrade_risk_check")
        if callable(risk_check):
            allowed, reason = risk_check(side, int(qty))
            if not allowed:
                logger.warning(
                    f"[Achilles] RISK REJECT {ticker}: {reason}, {side.upper()} x{qty} отклонён"
                )
                if ctx is not None:
                    with ctx.lock:
                        ctx.pending_orders.pop(ticker, None)
                        current = ctx.positions.get(ticker)
                        if current is not None and current.get("status") == "opening":
                            ctx.positions.pop(ticker, None)
                return False

        account_risk_check = params.get("_account_risk_check")
        if callable(account_risk_check):
            reject_reason = account_risk_check(
                side,
                int(qty),
                ticker=ticker,
                board=board,
                last_price=current_price,
            )
            if reject_reason:
                logger.warning(
                    f"[Achilles] ACCOUNT RISK REJECT {ticker}: {reject_reason}, "
                    f"{side.upper()} x{qty} отклонён"
                )
                if ctx is not None:
                    with ctx.lock:
                        ctx.pending_orders.pop(ticker, None)
                        current = ctx.positions.get(ticker)
                        if current is not None and current.get("status") == "opening":
                            ctx.positions.pop(ticker, None)
                return False

        reserve_capital = params.get("_reserve_capital")
        if callable(reserve_capital):
            reservation_key = reserve_capital(
                side,
                int(qty),
                ticker=ticker,
                board=board,
                last_price=current_price,
            )

    release_capital = params.get("_release_capital")

    def _release_reservation():
        if reservation_key and callable(release_capital):
            release_capital(reservation_key)

    _order_tid = [""]

    def _on_placed(order_id: str):
        _order_tid[0] = order_id or ""
        if ctx is not None and order_id:
            ctx.pending_orders[ticker] = {
                **ctx.pending_orders.get(ticker, {}),
                "tid": order_id,
            }

    def _on_filled_market(filled_qty: int, avg_price: float):
        """Callback для market-ордеров — запись сделки и обновление состояния."""
        _release_reservation()
        price = avg_price or _get_price(connector, board, ticker)
        if strategy_id:
            _record_fill(
                ticker=ticker, board=board, side=side, qty=qty,
                price=price, order_role="taker",
                strategy_id=strategy_id, connector_id=connector_id,
                comment=f"market filled={filled_qty}",
                fill_id=_order_tid[0],
            )
        if ctx is not None:
            with ctx.lock:
                ctx.pending_orders.pop(ticker, None)
                ctx.positions[ticker] = {
                    "side": side,
                    "qty": int(filled_qty) if filled_qty > 0 else int(qty),
                    "board": board,
                    "status": "open",
                }

    def _on_filled_chase(filled_qty: int, avg_price: float):
        """Callback для chase-ордеров — запись сделки и обновление состояния."""
        _release_reservation()
        if filled_qty > 0 and strategy_id:
            _record_fill(
                ticker=ticker, board=board, side=side,
                qty=filled_qty, price=avg_price,
                order_role="maker",
                strategy_id=strategy_id, connector_id=connector_id,
                comment=f"limit_book chase avg={avg_price:.4f}",
                fill_id=_order_tid[0],
            )
        if ctx is not None:
            with ctx.lock:
                ctx.pending_orders.pop(ticker, None)
                if is_close:
                    current = ctx.positions.get(ticker)
                    if current is not None:
                        remaining_qty = max(int(current.get("qty", qty)) - int(filled_qty), 0)
                        if remaining_qty > 0:
                            current["qty"] = remaining_qty
                            current["status"] = "open"
                        else:
                            ctx.positions.pop(ticker, None)
                else:
                    current = ctx.positions.get(ticker)
                    if current is None:
                        ctx.positions[ticker] = {
                            "side": side,
                            "qty": int(filled_qty),
                            "board": board,
                            "status": "open",
                        }
                    else:
                        current["side"] = side
                        current["qty"] = int(filled_qty)
                        current["board"] = board
                        current["status"] = "open"

    def _on_filled_limit_price(filled_qty: int, avg_price: float):
        """Callback для limit_price-ордеров — запись сделки и обновление состояния."""
        _release_reservation()
        if filled_qty > 0 and strategy_id:
            _record_fill(
                ticker=ticker, board=board, side=side,
                qty=filled_qty, price=avg_price,
                order_role="maker",
                strategy_id=strategy_id, connector_id=connector_id,
                comment=f"limit_price filled={filled_qty}",
                fill_id=_order_tid[0],
            )
        if ctx is not None:
            with ctx.lock:
                ctx.pending_orders.pop(ticker, None)
                if is_close:
                    current = ctx.positions.get(ticker)
                    if current is not None:
                        remaining_qty = max(int(current.get("qty", qty)) - int(filled_qty), 0)
                        if remaining_qty > 0:
                            current["qty"] = remaining_qty
                            current["status"] = "open"
                        else:
                            ctx.positions.pop(ticker, None)
                else:
                    if filled_qty > 0:
                        current = ctx.positions.get(ticker)
                        if current is None:
                            ctx.positions[ticker] = {
                                "side": side,
                                "qty": int(filled_qty),
                                "board": board,
                                "status": "open",
                            }
                        else:
                            current["side"] = side
                            current["qty"] = int(filled_qty)
                            current["board"] = board
                            current["status"] = "open"
                    else:
                        current = ctx.positions.get(ticker)
                        if current is not None and current.get("status") == "opening":
                            ctx.positions.pop(ticker, None)

    def _on_failed():
        """Callback при неудаче — откат состояния."""
        _release_reservation()
        if ctx is not None:
            with ctx.lock:
                ctx.pending_orders.pop(ticker, None)
                if is_close:
                    current = ctx.positions.get(ticker)
                    if current is not None and current.get("status") == "closing":
                        current["status"] = "open"
                else:
                    current = ctx.positions.get(ticker)
                    if current is not None and current.get("status") == "opening":
                        ctx.positions.pop(ticker, None)

    # Выбираем нужный callback в зависимости от режима
    try:
        if order_mode == "market":
            result = placer.place_with_state(
                account_id, board, ticker, side, qty,
                order_mode=order_mode,
                on_placed=_on_placed,
                on_filled=_on_filled_market,
                on_failed=_on_failed,
            )
        elif order_mode == "limit_book":
            result = placer.place_with_state(
                account_id, board, ticker, side, qty,
                order_mode=order_mode,
                on_placed=_on_placed,
                on_filled=_on_filled_chase,
                on_failed=_on_failed,
            )
        else:  # limit_price
            result = placer.place_with_state(
                account_id, board, ticker, side, qty,
                order_mode=order_mode,
                on_placed=_on_placed,
                on_filled=_on_filled_limit_price,
                on_failed=_on_failed,
            )
    except Exception:
        _release_reservation()
        raise

    if not result.success:
        _release_reservation()

    return result.success


def _do_snapshot(connector, params: dict, strategy_id: str):
    """Фиксируем референсные цены."""
    ctx = _get_state(strategy_id)
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
    with ctx.lock:
        ctx.reference_prices = new_prices


def _do_signal(connector, params: dict, account_id: str, strategy_id: str):
    """Считаем % изменение, входим в топ-рост и топ-падение."""
    ctx = _get_state(strategy_id)
    with ctx.lock:
        if not ctx.reference_prices:
            logger.warning("[Achilles] Нет референсных цен, сигнал пропущен")
            return
        ref_copy = dict(ctx.reference_prices)

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
                        strategy_id=strategy_id, connector_id=connector_id,
                        params=params)
            if ok:
                if order_mode == "market":
                    with ctx.lock:
                        ctx.positions[ticker] = {"side": "buy", "qty": actual_qty, "board": board, "status": "open"}
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
                        strategy_id=strategy_id, connector_id=connector_id,
                        params=params)
            if ok:
                if order_mode == "market":
                    with ctx.lock:
                        ctx.positions[ticker] = {"side": "sell", "qty": actual_qty, "board": board, "status": "open"}
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


def _do_close_limit(connector, params: dict, account_id: str, strategy_id: str):
    """Плановое закрытие лимитками."""
    ctx = _get_state(strategy_id)
    order_mode    = params.get("order_mode", "limit_book")
    spread_offset = float(params.get("spread_offset", 2.0))
    connector_id  = params.get("_connector_id", "")

    with ctx.lock:
        positions_snapshot = list(ctx.positions.items())

    for ticker, pos in positions_snapshot:
        if pos.get("status", "open") != "open":
            continue
        board      = pos.get("board", "TQBR")
        qty        = pos["qty"]
        close_side = "sell" if pos["side"] == "buy" else "buy"
        ok = _place(connector, account_id, board, ticker, close_side, qty,
                    order_mode, spread_offset,
                    strategy_id=strategy_id, connector_id=connector_id,
                    is_close=True, params=params)
        if ok:
            logger.info(f"[Achilles] CLOSE LIMIT {ticker}")
            with ctx.lock:
                if ticker in ctx.positions and order_mode == "market":
                    ctx.positions.pop(ticker, None)


def _do_close_market(connector, params: dict, account_id: str, strategy_id: str):
    """Аварийное закрытие рыночными ордерами."""
    from core.order_placer import OrderPlacer

    ctx = _get_state(strategy_id)
    connector_id = params.get("_connector_id", "")
    placer = OrderPlacer(connector, agent_name="Achilles")
    # Закрываем ВСЕ позиции, включая те что уже в статусе "closing"
    with ctx.lock:
        positions_snapshot = list(ctx.positions.items())

    for ticker, pos in positions_snapshot:
        board      = pos.get("board", "TQBR")
        qty        = pos["qty"]
        close_side = "sell" if pos["side"] == "buy" else "buy"
        try:
            result = placer.place_market(
                account_id=account_id,
                board=board,
                ticker=ticker,
                side=close_side,
                qty=qty,
                comment=f"close_market {ticker}",
            )
            if result.success:
                tid = result.order_id or ""
                logger.info(f"[Achilles] CLOSE MARKET {ticker} tid={tid}")
                if strategy_id:
                    price = _get_price(connector, board, ticker)
                    _record_fill(
                        ticker=ticker, board=board, side=close_side, qty=qty,
                        price=price, order_role="taker",
                        strategy_id=strategy_id, connector_id=connector_id,
                        comment=f"close_market tid={tid}",
                        fill_id=tid,
                    )
                with ctx.lock:
                    ctx.pending_orders.pop(ticker, None)
                    ctx.positions.pop(ticker, None)
            else:
                logger.error(f"[Achilles] CLOSE MARKET {ticker} — не удалось: {result.error}")
        except Exception as e:
            logger.error(f"[Achilles] CLOSE MARKET {ticker}: {e}")
