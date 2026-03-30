# strategies/bochka_cny.py
#
# Роль: Пробойная дневная стратегия на фьючерсе CNY (китайский юань).
# Логика: Если цена закрытия текущего бара пробивает Highest/Lowest за N баров
#         (значение индикатора взято со сдвигом otstup=2 бара назад) —
#         входим в направлении пробоя. Закрываем позицию по времени (time_close).
#
# Вызов: LiveEngine._process_bar() → on_precalc() → on_bar() → execute_signal()
# Потребители: BacktestEngine (бэктест), LiveEngine (реальная торговля)
#
# Особенности:
#   - on_bar() одинаков для бэктеста и реала: сигнал на закрытом баре
#   - execute_signal() реализует реальное исполнение через коннектор
#   - Для close стратегия использует подтверждённую брокерскую позицию,
#     а не локальное оптимистичное состояние

from loguru import logger


def get_info() -> dict:
    return {
        "name":        "Бочка CNY",
        "version":     "1.1",
        "author":      "Alexey",
        "description": "Пробой Highest/Lowest за период. Инструмент: CNY фьючерс.",
        "tickers":     ["CNY"],
    }


def get_params() -> dict:
    return {
        "ticker": {
            "type":        "ticker",
            "default":     "CNY",
            "label":       "Тикер",
            "description": "Торгуемый инструмент",
        },
        "board": {
            "type":        "str",
            "default":     "SPBFUT",
            "label":       "Борд",
            "description": "Торговая площадка (SPBFUT для фьючерсов)",
        },
        "period": {
            "type":        "int",
            "default":     300,
            "min":         10,
            "max":         2000,
            "label":       "Period",
            "description": "Период Highest/Lowest (баров)",
        },
        "otstup": {
            "type":        "int",
            "default":     2,
            "min":         1,
            "max":         50,
            "label":       "Otstup",
            "description": "Смещение назад для сравнения (shift индикатора)",
        },
        "time_open": {
            "type":        "time",
            "default":     708,
            "label":       "Time open",
            "description": "Начало торговли (708 = 11:48)",
        },
        "time_close": {
            "type":        "time",
            "default":     900,
            "label":       "Time close",
            "description": "Закрытие позиции (900 = 15:00)",
        },
        "time_limit": {
            "type":        "time",
            "default":     900,
            "label":       "Time limit",
            "description": "Ограничение времени торговли (900 = 15:00)",
        },
        "qty": {
            "type":        "int",
            "default":     1,
            "min":         1,
            "max":         1000,
            "label":       "Лотность",
            "description": "Количество контрактов",
        },
        "commission": {
            "type":        "commission",
            "default":     0.002,
            "min":         0.0,
            "max":         100.0,
            "label":       "Комиссия",
            "description": "Комиссия брокера (автоматически переключается между % и ₽ в зависимости от типа инструмента)",
        },
        "order_mode": {
            "type":        "select",
            "default":     "market",
            "options":     ["market", "limit", "limit_price"],
            "labels":      ["Рыночная", "Лимитная (стакан)", "Лимитная (цена)"],
            "label":       "Тип заявки",
            "description": "market — рыночная; limit — лимитка по bid/offer с автоперестановкой; limit_price — лимитка по last price до 23:45",
        },
    }


def get_indicators() -> list:
    return [
        {"col": "_highest", "type": "step", "color": "#a6e3a1", "label": "Highest", "linewidth": 1.0},
        {"col": "_lowest",  "type": "step", "color": "#f38ba8", "label": "Lowest",  "linewidth": 1.0},
    ]


def on_start(params: dict, connector) -> None:
    time_open = int(params.get('time_open', 708))
    time_close = int(params.get('time_close', 900))
    time_limit = int(params.get('time_limit', 900))
    if time_open >= time_close:
        logger.warning(
            f'[Бочка CNY] Некорректное окно времени: time_open={time_open} >= time_close={time_close}'
        )
    if time_open >= time_limit:
        logger.warning(
            f'[Бочка CNY] Некорректное окно времени: time_open={time_open} >= time_limit={time_limit}'
        )
    logger.info(f"[Бочка CNY] Запуск. Тикер: {params.get('ticker')}")


def on_stop(params: dict, connector) -> None:
    logger.info("[Бочка CNY] Остановка.")


def on_tick(tick_data: dict, params: dict, connector) -> None:
    """Не используется — стратегия работает на барах."""
    pass


# ── Индикаторы ────────────────────────────────────────────────────────────────

def get_lookback(params: dict) -> int:
    """Возвращает размер окна истории, необходимый стратегии."""
    period = int(params.get("period", 300))
    otstup = int(params.get("otstup", 2))
    return period + otstup + 5


def on_precalc(df, params: dict):
    """
    Предрасчёт индикаторов Highest/Lowest.
    Вызывается и в бэктесте (BacktestEngine), и в реале (LiveEngine._process_bar).

    shift(otstup) — значение индикатора на текущем баре равно
    max/min за period баров, взятому otstup баров назад.
    Т.е. при otstup=2: на баре [i] стоит значение с бара [i-2].
    """
    period = int(params.get("period", 300))
    otstup = int(params.get("otstup", 2))
    df["_highest"] = (
        df["high"].rolling(window=period, min_periods=period).max().shift(otstup)
    )
    df["_lowest"] = (
        df["low"].rolling(window=period, min_periods=period).min().shift(otstup)
    )
    return df


# ── Исключённые даты (бэктест + реал) ────────────────────────────────────────
# Формат: YYMMDD (int). 220224=24фев2022, 220225=25фев2022, 250617=17июн2025
_EXCLUDE_DATES = {220224, 220225, 250617}


# ── Сигнальная логика (общая для бэктеста и реала) ───────────────────────────

def on_bar(bars: list[dict], position: int, params: dict) -> dict:
    """
    Вызывается на каждом ЗАКРЫТОМ баре.

    В бэктесте: BacktestEngine вызывает на баре [i], исполнение по open[i+1].
    В реале:    LiveEngine вызывает при появлении нового закрытого бара,
                исполнение немедленно через execute_signal().

    Логика входа:
      - Текущий бар закрылся выше _highest (= Highest на баре [-2]) → BUY
      - Текущий бар закрылся ниже  _lowest  (= Lowest  на баре [-2]) → SELL
      - Только в окне time_open < time_min < time_limit
      - Только если нет открытой позиции

    Логика выхода:
      - Ровно в time_close, если есть позиция и день не выходной
    """
    current    = bars[-1]
    highest    = current.get("_highest")
    lowest     = current.get("_lowest")
    time_min   = current["time_min"]
    date_int   = current["date_int"]
    weekday    = current["weekday"]
    time_open  = int(params.get("time_open",  708))
    time_close = int(params.get("time_close", 900))
    time_limit = int(params.get("time_limit", 900))
    qty        = int(params.get("qty", 1))

    def _nan(v):
        try:
            return v != v
        except Exception:
            return True

    if highest is None or lowest is None or _nan(highest) or _nan(lowest):
        return {"action": None}

    # EXIT — приоритет над входом
    if position != 0 and _in_close_window(time_min, time_close) and weekday not in (6, 7):
        return {"action": "close", "qty": qty, "comment": "Close EOD"}

    if position != 0:
        return {"action": None}

    # ENTRY (поддержка overnight: окно может быть через полночь)
    if not _in_open_window(time_min, time_open, time_limit):
        return {"action": None}
    if date_int in _EXCLUDE_DATES:
        return {"action": None}

    close = current["close"]

    if close > highest:
        return {"action": "buy",  "qty": qty, "comment": f"Long > {highest:.4f}"}
    if close < lowest:
        return {"action": "sell", "qty": qty, "comment": f"Short < {lowest:.4f}"}

    return {"action": None}


# ── Реальное исполнение ───────────────────────────────────────────────────────

def execute_signal(signal: dict, connector, params: dict, account_id: str) -> None:
    """
    Вызывается LiveEngine вместо _execute_signal при наличии этой функции в модуле.
    Поддерживает все три режима заявок через параметр order_mode:
      - "market"      — рыночная заявка (по умолчанию)
      - "limit"       — лимитка по лучшей цене стакана (ChaseOrder)
      - "limit_price" — лимитка по last price (висит до исполнения или до 23:45)

    Для закрытия использует подтверждённую позицию из коннектора,
    а не локальное оптимистичное состояние.
    """
    action     = signal.get("action")
    qty        = int(signal.get("qty", 1))
    comment    = signal.get("comment", "")
    ticker     = params.get("ticker", "CNY")
    board      = params.get("board", "SPBFUT")
    order_mode = params.get("order_mode", "market")

    if action == "buy":
        _place(connector, account_id, board, ticker, "buy", qty, comment, order_mode)

    elif action == "sell":
        _place(connector, account_id, board, ticker, "sell", qty, comment, order_mode)

    elif action == "close":
        live_side, live_qty = _get_confirmed_position(connector, account_id, ticker, board)
        if live_side == 0 or live_qty <= 0:
            logger.warning('[Бочка CNY] close сигнал, но подтверждённая позиция отсутствует — пропуск')
            return
        close_side = "sell" if live_side > 0 else "buy"
        _place(connector, account_id, board, ticker, close_side, live_qty, comment, order_mode)


def _place(connector, account_id: str, board: str, ticker: str,
           side: str, qty: int, comment: str = "",
           order_mode: str = "market") -> bool:
    """
    Выставляет заявку через коннектор в зависимости от order_mode:
      - "market"      — рыночная заявка
      - "limit"       — ChaseOrder: лимитка по bid/offer с автоперестановкой
      - "limit_price" — лимитная по last price; мониторится до исполнения или 23:45
    """
    try:
        if order_mode == "limit":
            # Лимитка по лучшей цене стакана — ChaseOrder в фоновом потоке
            from core.chase_order import ChaseOrder
            import threading

            def _run_chase():
                chase = ChaseOrder(
                    connector=connector,
                    account_id=account_id,
                    ticker=ticker,
                    side=side,
                    quantity=qty,
                    board=board,
                    agent_name="Бочка CNY",
                )
                chase.wait(timeout=60)
                if not chase.is_done:
                    chase.cancel()
                if chase.filled_qty == 0:
                    logger.error(
                        f"[Бочка CNY] ОШИБКА заявки: агент=Бочка CNY тикер={ticker} "
                        f"сторона={side.upper()} qty={qty} цена=bid/offer "
                        f"вид=limit(стакан) — ничего не исполнено за 60 сек | {comment}"
                    )
                else:
                    logger.info(
                        f"[Бочка CNY] Chase {side.upper()} {ticker}x{qty} "
                        f"filled={chase.filled_qty} avg={chase.avg_price:.4f} | {comment}"
                    )

            t = threading.Thread(target=_run_chase, daemon=True,
                                 name=f"bochka-chase-{ticker}-{side}")
            t.start()
            return True

        elif order_mode == "limit_price":
            # Лимитная по last price — висит до исполнения или до 23:45
            import threading, time as _time
            from datetime import datetime as _dt

            price = _get_last_price(connector, board, ticker)
            if not price:
                logger.warning(f"[Бочка CNY] limit_price: нет цены для {ticker}, fallback market")
                order_mode = "market"
            else:
                tid = connector.place_order(
                    account_id=account_id,
                    ticker=ticker,
                    side=side,
                    quantity=qty,
                    order_type="limit",
                    price=price,
                    board=board,
                    agent_name="Бочка CNY",
                )
                if not tid:
                    logger.error(
                        f"[Бочка CNY] ОШИБКА заявки: агент=Бочка CNY тикер={ticker} "
                        f"сторона={side.upper()} qty={qty} цена={price} "
                        f"вид=limit_price — ордер не выставлен | {comment}"
                    )
                    return False

                logger.info(f"[Бочка CNY] LIMIT {side.upper()} {ticker}x{qty} @{price} tid={tid} | {comment}")

                from config.settings import TRADING_END_TIME_MIN
                _TERMINAL = {"matched", "cancelled", "canceled", "denied", "removed", "expired", "killed"}
                CANCEL_MIN = TRADING_END_TIME_MIN

                def _monitor():
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
                                    logger.info(f"[Бочка CNY] LIMIT tid={tid} {status} filled={filled}/{qty}")
                                    break
                        except Exception as e:
                            logger.warning(f"[Бочка CNY] monitor tid={tid}: {e}")

                        now_min = _dt.now().hour * 60 + _dt.now().minute
                        if now_min >= CANCEL_MIN:
                            logger.info(f"[Бочка CNY] LIMIT tid={tid} снимается в 23:45 (filled={filled}/{qty})")
                            try:
                                connector.cancel_order(tid, account_id)
                            except Exception:
                                pass
                            # Ждём финального статуса
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

                t = threading.Thread(target=_monitor, daemon=True,
                                     name=f"bochka-lp-{ticker}-{tid}")
                t.start()
                return True

        # market (включая fallback из limit_price при отсутствии цены)
        tid = connector.place_order(
            account_id=account_id,
            ticker=ticker,
            side=side,
            quantity=qty,
            order_type="market",
            board=board,
            agent_name="Бочка CNY",
        )
        if tid:
            logger.info(f"[Бочка CNY] {side.upper()} {ticker}x{qty} tid={tid} | {comment}")
            return True
        else:
            logger.error(
                f"[Бочка CNY] ОШИБКА заявки: агент=Бочка CNY тикер={ticker} "
                f"сторона={side.upper()} qty={qty} цена=market "
                f"вид=market — ордер не выставлен | {comment}"
            )
            return False

    except Exception as e:
        logger.error(f"[Бочка CNY] _place {ticker} {side}: {e}")
        return False


def _get_last_price(connector, board: str, ticker: str) -> float:
    """Возвращает last price для лимитной заявки по цене."""
    try:
        if hasattr(connector, "get_best_quote"):
            q = connector.get_best_quote(board, ticker)
            if q:
                return q.get("last") or q.get("bid") or q.get("offer") or 0.0
        if hasattr(connector, "get_last_price"):
            return connector.get_last_price(ticker, board) or 0.0
    except Exception as e:
        logger.warning(f"[Бочка CNY] _get_last_price {ticker}: {e}")
    return 0.0



def _get_confirmed_position(connector, account_id: str, ticker: str, board: str) -> tuple[int, int]:
    """Возвращает подтверждённую позицию из коннектора: (side, abs_qty)."""
    try:
        if not hasattr(connector, 'get_positions'):
            return 0, 0
        positions = connector.get_positions(account_id) or []
        for pos in positions:
            if pos.get('ticker') != ticker:
                continue
            pos_board = str(pos.get('board', board) or board)
            if pos_board != board:
                continue

            raw_qty = float(pos.get('quantity', 0) or 0)
            if raw_qty > 0:
                return 1, int(abs(raw_qty))
            if raw_qty < 0:
                return -1, int(abs(raw_qty))

            side = str(pos.get('side', '')).lower()
            qty = int(abs(float(pos.get('quantity', 0) or 0)))
            if side == 'buy' and qty > 0:
                return 1, qty
            if side == 'sell' and qty > 0:
                return -1, qty
    except Exception as e:
        logger.warning(f'[Бочка CNY] _get_confirmed_position {ticker}: {e}')
    return 0, 0
