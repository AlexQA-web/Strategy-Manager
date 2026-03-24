# strategies/example_strategy.py
"""Пример стандартной bar-based стратегии для Trading Manager."""

from loguru import logger


def get_info() -> dict:
    """Метаданные стратегии для UI."""
    return {
        'name': 'Example Strategy',
        'version': '2.0',
        'author': 'Alexey',
        'description': 'Пример bar-based стратегии на пересечении двух SMA без мгновенного реверса.',
        'tickers': ['SBER', 'GAZP'],
    }


def get_params() -> dict:
    """Описание параметров для UI-редактора."""
    return {
        'ticker': {
            'type': 'ticker',
            'default': 'SBER',
            'label': 'Тикер',
            'description': 'Инструмент для торговли',
        },
        'fast_period': {
            'type': 'int',
            'default': 20,
            'min': 2,
            'max': 500,
            'label': 'Быстрая SMA',
            'description': 'Период быстрой скользящей средней',
        },
        'slow_period': {
            'type': 'int',
            'default': 50,
            'min': 3,
            'max': 1000,
            'label': 'Медленная SMA',
            'description': 'Период медленной скользящей средней',
        },
        'qty': {
            'type': 'int',
            'default': 10,
            'min': 1,
            'max': 1000,
            'label': 'Лот',
            'description': 'Количество лотов в сигнале',
        },
        'time_open': {
            'type': 'time',
            'default': 600,
            'label': 'Начало входов',
            'description': 'Начало торгового окна (600 = 10:00)',
        },
        'time_close': {
            'type': 'time',
            'default': 1425,
            'label': 'Закрытие позиции',
            'description': 'Принудительное закрытие позиции (1425 = 23:45)',
        },
        'commission': {
            'type': 'commission',
            'default': 'auto',
            'label': 'Комиссия',
            'description': 'Комиссия брокера (auto = по настройкам приложения)',
        },
    }


def get_indicators() -> list:
    return [
        {'col': '_fast', 'type': 'line', 'color': '#89b4fa', 'label': 'Fast SMA', 'linewidth': 1.2},
        {'col': '_slow', 'type': 'line', 'color': '#f9e2af', 'label': 'Slow SMA', 'linewidth': 1.2},
    ]


def get_lookback(params: dict) -> int:
    fast_period = int(params.get('fast_period', 20))
    slow_period = int(params.get('slow_period', 50))
    return max(fast_period, slow_period) + 10


def on_start(params: dict, connector) -> None:
    logger.info(f"[Example Strategy] Запуск. Тикер: {params.get('ticker')}")


def on_stop(params: dict, connector) -> None:
    logger.info('[Example Strategy] Остановка.')


def on_tick(tick_data: dict, params: dict, connector) -> None:
    """В стандартной bar-based стратегии тики обычно не используются."""
    pass


def on_precalc(df, params: dict):
    """Считает быстрые и медленные SMA."""
    fast_period = int(params.get('fast_period', 20))
    slow_period = int(params.get('slow_period', 50))

    df['_fast'] = df['close'].rolling(window=fast_period, min_periods=fast_period).mean()
    df['_slow'] = df['close'].rolling(window=slow_period, min_periods=slow_period).mean()
    return df


def on_bar(bars: list[dict], position: int, params: dict) -> dict:
    """
    Пример логики:
    - вход по пересечению fast/slow SMA
    - новые входы только внутри окна time_open <= time < time_close
    - при противоположном сигнале сначала только close
    - без мгновенного reverse через qty * 2
    """
    if len(bars) < 2:
        return {'action': None}

    prev = bars[-2]
    cur = bars[-1]

    time_min = cur['time_min']
    weekday = cur['weekday']
    qty = int(params.get('qty', 10))
    time_open = int(params.get('time_open', 600))
    time_close = int(params.get('time_close', 1425))

    prev_fast = prev.get('_fast')
    prev_slow = prev.get('_slow')
    fast = cur.get('_fast')
    slow = cur.get('_slow')

    def _bad(value) -> bool:
        if value is None:
            return True
        try:
            return value != value
        except Exception:
            return True

    if any(_bad(v) for v in (prev_fast, prev_slow, fast, slow)):
        return {'action': None}

    if weekday in (6, 7):
        return {'action': None}

    crossed_up = prev_fast <= prev_slow and fast > slow
    crossed_down = prev_fast >= prev_slow and fast < slow

    if position != 0 and time_min >= time_close:
        return {'action': 'close', 'qty': qty, 'comment': f'Close by time {time_min}'}

    if position == 1 and crossed_down:
        return {'action': 'close', 'qty': qty, 'comment': 'Close long before possible short'}

    if position == -1 and crossed_up:
        return {'action': 'close', 'qty': qty, 'comment': 'Close short before possible long'}

    if not (time_open <= time_min < time_close):
        return {'action': None}

    if position == 0 and crossed_up:
        return {'action': 'buy', 'qty': qty, 'comment': 'Fast SMA crossed above slow SMA'}

    if position == 0 and crossed_down:
        return {'action': 'sell', 'qty': qty, 'comment': 'Fast SMA crossed below slow SMA'}

    return {'action': None}
