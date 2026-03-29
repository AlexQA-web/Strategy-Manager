# strategies/tracker.py
# TRACKER — канальная стратегия SMA + ATR с лимитными заявками
# Портировано из TSLab TRACKER_ЛИМИТКИ_Si.tscript

import pandas as pd
import numpy as np
from loguru import logger


def get_info() -> dict:
    return {
        "name":        "Tracker",
        "version":     "1.1",
        "author":      "Alexey",
        "description": "Канал SMA ± ATR*K. Вход по сигналу при пробое канала на старшем ТФ "
                       "с подтверждением на 1-минутном. Выход при возврате в канал. "
                       "Тип ордера определяется в настройках агента.",
        "tickers":     ["SiH6", "SiM6"],
    }


def get_params() -> dict:
    return {
        "ticker": {
            "type": "ticker", "default": "SiH6",
            "label": "Тикер",
            "description": "Торгуемый инструмент",
        },
        "compress_tf": {
            "type": "int", "default": 15, "min": 1, "max": 240,
            "label": "Старший ТФ (мин)",
            "description": "Таймфрейм для SMA и ATR (минуты). Основной ТФ агента должен быть 1м.",
        },
        "sma_period": {
            "type": "int", "default": 100, "min": 5, "max": 2000,
            "label": "Период SMA",
            "description": "Период скользящей средней на старшем ТФ",
        },
        "atr_period": {
            "type": "int", "default": 20, "min": 2, "max": 500,
            "label": "Период ATR",
            "description": "Период ATR на старшем ТФ",
        },
        "k": {
            "type": "float", "default": 1.0, "min": 0.1, "max": 10.0, "step": 0.1,
            "label": "К (множитель ATR)",
            "description": "Ширина канала: SMA ± ATR * K",
        },
        "time_open": {
            "type": "time", "default": 630,
            "label": "Время входа (мин)",
            "description": "Начало торговли в минутах от полуночи (630 = 10:30)",
        },
        "close_friday_enabled": {
            "type": "bool", "default": False,
            "label": "Закрывать в пятницу?",
            "description": "Принудительно закрывать позицию в пятницу в указанное время"
        },
        "friday_close": {
            "type": "time", "default": 1005,
            "label": "Время закрытия в пятницу (мин)",
            "description": "Принудительное закрытие в пятницу (1005 = 16:45)",
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
        {"col": "_sma",        "type": "line", "color": "#89b4fa", "label": "SMA",      "linewidth": 1.2},
        {"col": "_buy_level",  "type": "line", "color": "#a6e3a1", "label": "Buy lvl",  "linewidth": 1.0, "linestyle": "--"},
        {"col": "_sell_level", "type": "line", "color": "#f38ba8", "label": "Sell lvl", "linewidth": 1.0, "linestyle": "--"},
    ]


def on_start(params: dict, connector) -> bool:
    logger.info(f"[Tracker] Запуск. Тикер: {params.get('ticker')}")
    return True


def on_stop(params: dict, connector) -> None:
    logger.info("[Tracker] Остановка.")


def on_tick(tick_data: dict, params: dict, connector) -> None:
    pass


# ── Бэктест / LiveEngine логика ──────────────────────────────────────────────

def get_lookback(params: dict) -> int:
    compress_tf = int(params.get("compress_tf", 15))
    sma_period = int(params.get("sma_period", 100))
    atr_period = int(params.get("atr_period", 20))
    # Нужно достаточно 1-минутных баров для расчёта на старшем ТФ
    return (max(sma_period, atr_period) + 10) * compress_tf + 100


def on_precalc(df, params: dict):
    """Считает SMA и ATR на сжатом ТФ, разворачивает обратно на 1-минутный."""
    compress_tf = int(params.get("compress_tf", 15))
    sma_period = int(params.get("sma_period", 100))
    atr_period = int(params.get("atr_period", 20))
    k = float(params.get("k", 1.0))

    # Сжимаем в старший ТФ по реальному времени (date_int + окно времени).
    # Группировка по порядковому индексу (_bar_idx // compress_tf) смешивает
    # бары разных торговых сессий на границах дней (ночной перерыв, клиринг),
    # что даёт неверные high/low и искажает ATR/SMA.
    df = df.copy()
    df["_tf_group"] = (
        df["date_int"].astype(str) + "_" +
        (df["time_min"] // compress_tf).astype(str)
    )

    # OHLC на старшем ТФ
    tf = df.groupby("_tf_group").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    )

    # SMA на close старшего ТФ
    tf["_sma"] = tf["close"].rolling(window=sma_period, min_periods=sma_period).mean()

    # ATR на старшем ТФ
    tf["_prev_close"] = tf["close"].shift(1)
    tf["_tr"] = np.maximum(
        tf["high"] - tf["low"],
        np.maximum(
            abs(tf["high"] - tf["_prev_close"]),
            abs(tf["low"] - tf["_prev_close"]),
        )
    )
    tf["_atr"] = tf["_tr"].rolling(window=atr_period, min_periods=atr_period).mean()

    # Уровни канала
    tf["_buy_level"] = tf["_sma"] + tf["_atr"] * k
    tf["_sell_level"] = tf["_sma"] - tf["_atr"] * k

    # Разворачиваем обратно на 1-минутный ТФ
    df = df.merge(
        tf[["_sma", "_atr", "_buy_level", "_sell_level"]],
        left_on="_tf_group", right_index=True, how="left",
    )

    return df


def on_bar(bars: list[dict], position: int, params: dict) -> dict:
    """
    Канальная система без мгновенного реверса в один бар:
    - close > buy_level  → лонг, если позиции нет
    - close < sell_level → шорт, если позиции нет
    - при противоположном сигнале сначала только закрываем текущую позицию
    - новый вход возможен уже на следующем баре
    """
    if not bars:
        return {"action": None}

    cur        = bars[-1]
    time_min   = cur["time_min"]
    weekday    = cur["weekday"]
    close      = cur["close"]
    buy_level  = cur.get("_buy_level")
    sell_level = cur.get("_sell_level")

    time_open    = int(params.get("time_open",    630))
    friday_close = int(params.get("friday_close", 1005))
    close_friday_enabled = bool(params.get("close_friday_enabled", False))
    qty          = int(params.get("qty", 1))

    if buy_level is None or sell_level is None or buy_level != buy_level or sell_level != sell_level:
        return {"action": None}

    # Принудительное закрытие только в пятницу (если включено)
    if position != 0 and close_friday_enabled and weekday == 5 and time_min >= friday_close:
        return {"action": "close", "qty": qty, "comment": "Close Friday"}

    if time_min < time_open or weekday in (6, 7):
        return {"action": None}

    # close выше buy_level → нужен лонг
    if close > buy_level:
        if position == 0:
            return {"action": "buy", "qty": qty,
                    "comment": f"Long: cls={close:.2f} > buy={buy_level:.2f}"}
        if position == -1:
            return {"action": "close", "qty": qty,
                    "comment": f"Close short before long: cls={close:.2f} > buy={buy_level:.2f}"}

    # close ниже sell_level → нужен шорт (не в пятницу)
    elif close < sell_level and weekday != 5:
        if position == 0:
            return {"action": "sell", "qty": qty,
                    "comment": f"Short: cls={close:.2f} < sell={sell_level:.2f}"}
        if position == 1:
            return {"action": "close", "qty": qty,
                    "comment": f"Close long before short: cls={close:.2f} < sell={sell_level:.2f}"}

    return {"action": None}
