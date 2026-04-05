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


# ── Вспомогательные функции для проверки временных окон ────────────────────────

def _in_open_window(time_min: int, time_open: int, time_limit: int) -> bool:
    """Проверяет, находится ли time_min в окне торговли (с учётом перехода через полночь)."""
    if time_open < time_limit:
        # Обычный случай: окно в пределах одного дня
        return time_open < time_min < time_limit
    else:
        # Окно через полночь (например, 2300–0200)
        return time_min > time_open or time_min < time_limit


def _in_close_window(time_min: int, time_close: int) -> bool:
    """Проверяет, наступило ли время закрытия позиции."""
    return time_min >= time_close


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
    from core.order_placer import OrderPlacer

    action     = signal.get("action")
    qty        = int(signal.get("qty", 1))
    comment    = signal.get("comment", "")
    ticker     = params.get("ticker", "CNY")
    board      = params.get("board", "SPBFUT")
    order_mode = params.get("order_mode", "market")

    placer = OrderPlacer(connector, agent_name="Бочка CNY")

    if action == "buy":
        placer.place(account_id, board, ticker, "buy", qty, order_mode, comment)

    elif action == "sell":
        placer.place(account_id, board, ticker, "sell", qty, order_mode, comment)

    elif action == "close":
        live_side, live_qty = _get_confirmed_position(connector, account_id, ticker, board)
        if live_side == 0 or live_qty <= 0:
            logger.warning('[Бочка CNY] close сигнал, но подтверждённая позиция отсутствует — пропуск')
            return
        close_side = "sell" if live_side > 0 else "buy"
        placer.place(account_id, board, ticker, close_side, live_qty, order_mode, comment)


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
