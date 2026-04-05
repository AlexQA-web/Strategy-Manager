# strategies/valera_trend.py
# VALERA_TREND — трендовая стратегия на основе SMA
# Портировано из TSLab VALERA_TREND_Si.tscript

from loguru import logger

from core.indicators import is_nan


def get_info() -> dict:
    return {
        "name":        "Valera Trend",
        "version":     "1.0",
        "author":      "Alexey",
        "description": "Вход в направлении тренда по SMA в заданное время. "
                       "Фильтр: N баров подряд полностью по одну сторону от SMA (без касания). "
                       "Выход по времени или при пересечении SMA.",
        "tickers":     ["SiH6", "SiM6"],
    }


def get_params() -> dict:
    return {
        "ticker": {
            "type": "ticker", "default": "SiH6",
            "label": "Тикер",
            "description": "Торгуемый инструмент",
        },
        "sma_period": {
            "type": "int", "default": 200, "min": 5, "max": 2000,
            "label": "Период SMA",
            "description": "Период скользящей средней",
        },
        "candles": {
            "type": "int", "default": 12, "min": 1, "max": 200,
            "label": "Баров фильтра",
            "description": "Минимальное кол-во баров подряд полностью по одну сторону от SMA",
        },
        "time_open": {
            "type": "time", "default": 830,
            "label": "Время входа (мин)",
            "description": "Время входа в минутах от полуночи (830 = 13:50)",
        },
        "time_close": {
            "type": "time", "default": 810,
            "label": "Время выхода (мин)",
            "description": "Время выхода в минутах от полуночи (810 = 13:30). Позиция закрывается на следующий день.",
        },
        "qty": {
            "type": "int", "default": 1, "min": 1, "max": 100,
            "label": "Лот",
            "description": "Кол-во контрактов (при статическом лоте)",
        },
        "commission": {
            "type": "commission", "default": 10.0, "min": 0.0, "max": 1000.0,
            "label": "Комиссия",
            "description": "Комиссия брокера (автоматически переключается между % и ₽ в зависимости от типа инструмента)",
        },
    }


def get_indicators() -> list:
    return [
        {"col": "_sma", "type": "line", "color": "#89b4fa", "label": "SMA", "linewidth": 1.2},
    ]


def on_start(params: dict, connector) -> bool:
    logger.info(f"[Valera Trend] Запуск. Тикер: {params.get('ticker')}")
    return True


def on_stop(params: dict, connector) -> None:
    logger.info("[Valera Trend] Остановка.")


def on_tick(tick_data: dict, params: dict, connector) -> None:
    pass


# ── Бэктест / LiveEngine логика ──────────────────────────────────────────────

def get_lookback(params: dict) -> int:
    sma_period = int(params.get("sma_period", 200))
    candles = int(params.get("candles", 12))
    return sma_period + candles + 10


def on_precalc(df, params: dict):
    """Считает SMA и счётчик баров фильтра."""
    sma_period = int(params.get("sma_period", 200))

    df["_sma"] = df["close"].rolling(window=sma_period, min_periods=sma_period).mean()

    # check[i] = True если бар i-1 полностью по одну сторону от SMA
    # и close[i] по ту же сторону
    # Бар полностью выше SMA: low[-1] > sma[-1] && close > sma
    # Бар полностью ниже SMA: high[-1] < sma[-1] && close < sma
    sma = df["_sma"]
    high = df["high"]
    low = df["low"]
    close = df["close"]

    above = (low.shift(1) > sma.shift(1)) & (close > sma)
    below = (high.shift(1) < sma.shift(1)) & (close < sma)
    check = above | below

    # Счётчик: сколько баров подряд check=True
    # Сбрасывается при check=False
    counter = [0] * len(df)
    for i in range(1, len(df)):
        if check.iloc[i]:
            counter[i] = counter[i - 1] + 1
        else:
            counter[i] = 0

    df["_candle_count"] = counter
    return df


def on_bar(bars: list[dict], position: int, params: dict) -> dict:
    """
    Логика VALERA_TREND:
    - Лонг:  time == time_open && open > sma && candle_count >= candles && !pos && не пятница/суббота/воскресенье
    - Шорт:  time == time_open && open < sma && candle_count >= candles && !pos && не пятница
    - Выход: позиция держится ~24ч, закрывается в окне [time_close, time_open)
      Пример: вход 13:50 (time_open=830), выход 13:30 следующего дня (time_close=810)
    """
    if len(bars) < 2:
        return {"action": None}

    cur = bars[-1]
    time_min = cur["time_min"]
    weekday = cur["weekday"]
    close = cur["close"]
    open_ = cur["open"]

    sma = cur.get("_sma")
    candle_count = cur.get("_candle_count", 0)

    time_open = int(params.get("time_open", 600))
    time_close = int(params.get("time_close", 1425))
    candles_min = int(params.get("candles", 12))
    qty = int(params.get("qty", 1))

    if is_nan(sma):
        return {"action": None}

    filtr = candle_count >= candles_min

    # Выход по времени: окно закрытия [time_close, time_open)
    # Поддерживает overnight-окна (time_close > time_open),
    # например time_close=1425 (23:45), time_open=600 (10:00)
    if position != 0:
        if time_close <= time_open:
            # Обычное окно в пределах одного дня
            in_close_window = time_close <= time_min < time_open
        else:
            # Overnight-окно: закрываем после time_close ИЛИ до time_open
            in_close_window = time_min >= time_close or time_min < time_open
        if in_close_window:
            return {"action": "close", "qty": qty, "comment": f"Close by time window {time_min}"}

    # Вход — только в заданное время
    if time_min != time_open:
        return {"action": None}

    if position == 0 and filtr:
        # Лонг: не пятница, суббота, воскресенье
        if open_ > sma and weekday not in (5, 6, 7):
            return {"action": "buy", "qty": qty,
                    "comment": f"Long open={open_:.2f} > sma={sma:.2f} cnt={candle_count}"}
        # Шорт: не пятница
        if open_ < sma and weekday not in (5,):
            return {"action": "sell", "qty": qty,
                    "comment": f"Short open={open_:.2f} < sma={sma:.2f} cnt={candle_count}"}

    return {"action": None}
