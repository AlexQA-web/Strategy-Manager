# strategies/daytrend.py
# Пробой дневного диапазона (DAYTREND)
# Портировано из TSLab

import math
from loguru import logger


def get_info() -> dict:
    return {
        "name":        "DayTrend",
        "version":     "1.0",
        "author":      "Alexey",
        "description": "Пробой сессионного хай/лоу с коэффициентом расширения. "
                       "Лонг при Close > SessionHigh + Range*K, шорт при Close < SessionLow - Range*K. "
                       "Стоп: Low[-1] - stop_long / уровень_шорта + stop_short.",
        "tickers":     ["SiZ5", "RIZ5"],
    }


def get_params() -> dict:
    return {
        "ticker": {
            "type": "ticker", "default": "SiZ5",
            "label": "Тикер",
            "description": "Торгуемый инструмент",
        },
        "k_long": {
            "type": "float", "default": 0.5, "min": 0.0, "max": 5.0, "step": 0.05,
            "label": "K лонга",
            "description": "Коэффициент расширения диапазона для входа в лонг",
        },
        "k_short": {
            "type": "float", "default": 0.5, "min": 0.0, "max": 5.0, "step": 0.05,
            "label": "K шорта",
            "description": "Коэффициент расширения диапазона для входа в шорт",
        },
        "stop_long": {
            "type": "float", "default": 100.0, "min": 0.0, "max": 10000.0, "step": 10.0,
            "label": "Стоп лонг",
            "description": "Отступ стопа для лонга (пункты от Low[-1])",
        },
        "stop_short": {
            "type": "float", "default": 100.0, "min": 0.0, "max": 10000.0, "step": 10.0,
            "label": "Стоп шорт",
            "description": "Отступ стопа для шорта (пункты от уровня входа)",
        },
        "qty": {
            "type": "int", "default": 1, "min": 1, "max": 100,
            "label": "Лот",
            "description": "Кол-во контрактов",
        },
        "time_start": {
            "type": "int", "default": 605, "min": 0, "max": 1439,
            "label": "Время входа (мин)",
            "description": "Начало торговли в минутах от полуночи (605 = 10:05)",
        },
        "time_end": {
            "type": "int", "default": 1080, "min": 0, "max": 1439,
            "label": "Время стоп входов (мин)",
            "description": "Окончание входов в минутах от полуночи (1080 = 18:00)",
        },
        "commission": {
            "type": "float", "default": 7.0, "min": 0.0, "max": 1000.0,
            "label": "Комиссия",
            "description": "Руб. на контракт за сделку",
        },
    }


def get_indicators() -> list:
    return [
        {"col": "_level_long",  "type": "step", "color": "#a6e3a1", "label": "Long lvl",  "linewidth": 1.0},
        {"col": "_level_short", "type": "step", "color": "#f38ba8", "label": "Short lvl", "linewidth": 1.0},
        {"col": "_prev_high",   "type": "step", "color": "#a6e3a1", "label": "Prev High", "linewidth": 0.7, "linestyle": ":"},
        {"col": "_prev_low",    "type": "step", "color": "#f38ba8", "label": "Prev Low",  "linewidth": 0.7, "linestyle": ":"},
    ]


def on_start(params: dict, connector) -> None:
    logger.info(f"[DayTrend] Запуск. Тикер: {params.get('ticker')}")


def on_stop(params: dict, connector) -> None:
    logger.info("[DayTrend] Остановка.")


def on_tick(tick_data: dict, params: dict, connector) -> None:
    pass


# ── Бэктест / LiveEngine логика ─────────────────────────────────────────────

def get_lookback(params: dict) -> int:
    return 50


def on_precalc(df, params: dict):
    """Считает уровни входа по хай/лоу ПРЕДЫДУЩЕЙ сессии.

    level_long  = prev_high + (prev_high - prev_low) * k_long
    level_short = prev_low  - (prev_high - prev_low) * k_short
    """
    k_long = float(params.get("k_long", 0.5))
    k_short = float(params.get("k_short", 0.5))

    # Хай/лоу каждой сессии (торгового дня)
    daily = df.groupby("date_int").agg(
        session_high=("high", "max"),
        session_low=("low", "min"),
    )
    # Сдвигаем на 1 день — получаем хай/лоу предыдущей сессии
    daily["_prev_high"] = daily["session_high"].shift(1)
    daily["_prev_low"] = daily["session_low"].shift(1)
    daily["_range"] = daily["_prev_high"] - daily["_prev_low"]
    daily["_level_long"] = daily["_prev_high"] + daily["_range"] * k_long
    daily["_level_short"] = daily["_prev_low"] - daily["_range"] * k_short

    # Мержим обратно в df по date_int
    df = df.merge(
        daily[["_prev_high", "_prev_low", "_level_long", "_level_short"]],
        left_on="date_int", right_index=True, how="left",
    )

    return df


def on_bar(bars: list[dict], position: int, params: dict) -> dict:
    """
    Логика DAYTREND:
    - Лонг: Close > SessionHigh + Range * k_long
    - Шорт: Close < SessionLow  - Range * k_short
    - Стоп лонг: Low[-1] - stop_long
    - Стоп шорт: уровень_шорта + stop_short
    - Реверс: противоположный сигнал закрывает текущую позицию
    - Торговля Пн-Пт, 10:05 — 18:00
    """
    if len(bars) < 2:
        return {"action": None}

    cur = bars[-1]
    prev = bars[-2]
    close = cur["close"]
    time_min = cur["time_min"]
    weekday = cur["weekday"]

    time_start = int(params.get("time_start", 605))
    time_end = int(params.get("time_end", 1080))
    stop_long = float(params.get("stop_long", 100.0))
    stop_short = float(params.get("stop_short", 100.0))
    qty = int(params.get("qty", 1))

    level_long = cur.get("_level_long")
    level_short = cur.get("_level_short")

    def _bad(v):
        if v is None:
            return True
        try:
            return math.isnan(v)
        except (TypeError, ValueError):
            return True

    if _bad(level_long) or _bad(level_short):
        return {"action": None}

    # Фильтр: только будни
    if weekday in (6, 7):
        return {"action": None}

    # Стоп-лосс проверка (до входов)
    if position == 1:
        stop_price = prev["low"] - stop_long
        if close <= stop_price:
            return {"action": "close", "qty": qty,
                    "comment": f"StopLong {close:.2f} <= {stop_price:.2f}"}

    if position == -1:
        stop_price = level_short + stop_short
        if close >= stop_price:
            return {"action": "close", "qty": qty,
                    "comment": f"StopShort {close:.2f} >= {stop_price:.2f}"}

    # Реверс: противоположный сигнал закрывает позицию
    if position == 1 and close < level_short:
        return {"action": "close", "qty": qty,
                "comment": f"Reverse: Close {close:.2f} < ShortLvl {level_short:.2f}"}

    if position == -1 and close > level_long:
        return {"action": "close", "qty": qty,
                "comment": f"Reverse: Close {close:.2f} > LongLvl {level_long:.2f}"}

    # Вне торгового окна — не входим
    if not (time_start < time_min < time_end):
        return {"action": None}

    # Вход
    if position == 0:
        if close > level_long:
            return {"action": "buy", "qty": qty,
                    "comment": f"Long: {close:.2f} > {level_long:.2f}"}
        if close < level_short:
            return {"action": "sell", "qty": qty,
                    "comment": f"Short: {close:.2f} < {level_short:.2f}"}

    return {"action": None}
