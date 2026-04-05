# strategies/_template.py
#
# Канонический шаблон стандартной bar-based стратегии для Trading Manager.
# Скопируй файл, переименуй и замени примерную логику своей.
#
# Контракт стратегии:
# - LiveEngine вызывает on_precalc() -> on_bar() -> стандартное исполнение сигнала
# - BacktestEngine вызывает on_precalc() -> on_bar() и исполняет сигнал на следующем баре
# - on_bar() должна возвращать только сигнал вида {'action': ..., 'qty': ..., 'comment': ...}
# - прямую отправку ордеров через connector в обычной стратегии не использовать
# - реверс через qty * 2 не использовать: сначала close, затем новый вход на следующем баре
# - для intraday-стратегий закрытие по времени обычно делают через time_min >= time_close
# - для overnight-стратегий окно закрытия нужно описывать явно, без слепого шаблона

import pandas as pd
from loguru import logger

# ── Глобальное состояние (если нужно) ────────────────────────────────────────
# Сбрасывай в on_start() через reset_state().
# Если состояние меняется из нескольких потоков, защищай его lock-ом.

_last_signal: str = ''


def reset_state() -> None:
    """Сбрасывает внутреннее состояние стратегии."""
    global _last_signal
    _last_signal = ''


# ── Метаданные ────────────────────────────────────────────────────────────────

def get_info() -> dict:
    return {
        'name': 'Название стратегии',
        'version': '1.0',
        'author': 'Автор',
        'description': 'Краткое описание логики стратегии.',
        'tickers': ['SiM6'],
    }


# ── Схема параметров ──────────────────────────────────────────────────────────
# Часто используемые типы: str, int, float, bool, time, select, ticker,
# instruments, commission, timeframe.

def get_params() -> dict:
    return {
        'ticker': {
            'type': 'ticker',
            'default': 'SiM6',
            'label': 'Тикер',
            'description': 'Торгуемый инструмент',
        },
        'qty': {
            'type': 'int',
            'default': 1,
            'min': 1,
            'max': 1000,
            'label': 'Лотность',
            'description': 'Количество контрактов в сигнале',
        },
        'time_open': {
            'type': 'time',
            'default': 600,
            'label': 'Время входа',
            'description': 'Пример времени входа (600 = 10:00)',
        },
        'time_close': {
            'type': 'time',
            'default': 1425,
            'label': 'Время выхода',
            'description': 'Пример времени закрытия (1425 = 23:45)',
        },
        'order_mode': {
            'type': 'select',
            'default': 'market',
            'options': ['market', 'limit', 'limit_price'],
            'labels': ['Рыночная', 'Лимитная (стакан)', 'Лимитная (цена)'],
            'label': 'Тип заявки',
            'description': 'Если стратегия использует стандартное исполнение движка',
        },
        'commission': {
            'type': 'commission',
            'default': 'auto',
            'label': 'Комиссия',
            'description': 'Комиссия брокера (auto = по настройкам приложения)',
        },
        # Добавь свои параметры ниже.
        # 'period': {
        #     'type': 'int',
        #     'default': 20,
        #     'min': 2,
        #     'max': 500,
        #     'label': 'Период',
        #     'description': 'Период индикатора',
        # },
    }


# ── Индикаторы для графика ────────────────────────────────────────────────────
# Колонки из on_precalc(), начинающиеся с '_', можно показать на графике.
# type: 'line' | 'step' | 'histogram'

def get_indicators() -> list:
    return [
        # {'col': '_fast', 'type': 'line', 'color': '#89b4fa', 'label': 'Fast', 'linewidth': 1.2},
        # {'col': '_slow', 'type': 'line', 'color': '#f9e2af', 'label': 'Slow', 'linewidth': 1.2},
    ]


# ── Размер окна истории ───────────────────────────────────────────────────────

def get_lookback(params: dict) -> int:
    """Возвращает минимальный lookback в барах для расчёта индикаторов."""
    # period = int(params.get('period', 20))
    # return period + 10
    return 200


# ── Жизненный цикл ────────────────────────────────────────────────────────────

def on_start(params: dict, connector) -> None:
    reset_state()
    logger.info(f"[Template] Запуск. Тикер: {params.get('ticker')}")


def on_stop(params: dict, connector) -> None:
    logger.info('[Template] Остановка.')


def on_tick(tick_data: dict, params: dict, connector) -> None:
    """Для обычной bar-based стратегии обычно не используется."""
    pass


# ── Предрасчёт индикаторов ────────────────────────────────────────────────────
# Предпочтительно использовать pandas-операции: rolling, shift, groupby, merge.
# Тяжёлые циклы по всей истории лучше избегать без необходимости.

def on_precalc(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """
    Рассчитывает индикаторы для всей истории.

    Входные колонки обычно содержат:
    open, high, low, close, vol, date_int, time_min, weekday.

    Возвращай исходный DataFrame с добавленными колонками, начинающимися с '_'.
    """
    # Пример: простая скользящая средняя.
    # period = int(params.get('period', 20))
    # df['_sma'] = df['close'].rolling(window=period, min_periods=period).mean()

    # Пример: уровни предыдущей сессии.
    # daily = df.groupby('date_int').agg(
    #     session_high=('high', 'max'),
    #     session_low=('low', 'min'),
    # )
    # daily['_prev_high'] = daily['session_high'].shift(1)
    # df = df.merge(daily[['_prev_high']], left_on='date_int', right_index=True, how='left')

    return df


# ── Торговая логика ───────────────────────────────────────────────────────────

def on_bar(bars: list[dict], position: int, params: dict) -> dict:
    """
    Вызывается на каждом закрытом баре.

    Возвращаемый контракт:
    - {'action': None}
    - {'action': 'buy', 'qty': int, 'comment': str}
    - {'action': 'sell', 'qty': int, 'comment': str}
    - {'action': 'close', 'qty': int, 'comment': str}

    Если нужен reverse, сначала возвращай 'close', а новый вход делай уже на
    следующем баре. Не используй reverse через qty * 2.
    """
    if len(bars) < 2:
        return {'action': None}

    current = bars[-1]
    prev = bars[-2]

    time_min = current['time_min']
    weekday = current['weekday']
    close = current['close']

    _ = prev, close  # Убери, когда используешь переменные в своей логике.

    time_open = int(params.get('time_open', 600))
    time_close = int(params.get('time_close', 1425))
    qty = int(params.get('qty', 1))

    # Фильтр: только будни.
    if weekday in (6, 7):
        return {'action': None}

    # Пример intraday-выхода по времени.
    # Для overnight-стратегии это условие нужно заменить на собственное окно.
    if position != 0 and time_min >= time_close:
        return {'action': 'close', 'qty': qty, 'comment': f'Close by time {time_min}'}

    # Пример точечного входа только на баре time_open.
    # Если стратегия использует окно входа, задай его явно своей логикой.
    if position != 0 or time_min != time_open:
        return {'action': None}

    # TODO: Добавь свою логику сигнала.
    # Пример:
    # fast = current.get('_fast')
    # slow = current.get('_slow')
    # prev_fast = prev.get('_fast')
    # prev_slow = prev.get('_slow')
    # if any(v is None or v != v for v in (fast, slow, prev_fast, prev_slow)):
    #     return {'action': None}
    # if prev_fast <= prev_slow and fast > slow:
    #     return {'action': 'buy', 'qty': qty, 'comment': 'Fast crossed above slow'}
    # if prev_fast >= prev_slow and fast < slow:
    #     return {'action': 'sell', 'qty': qty, 'comment': 'Fast crossed below slow'}

    return {'action': None}


# ── Нестандартное реальное исполнение (опционально) ──────────────────────────
# Если execute_signal() не определена, LiveEngine использует стандартное
# исполнение сам. Добавляй execute_signal() только если действительно нужен
# special-case: например, мультиинструментальная стратегия или свой lifecycle
# лимитных заявок.
#
# ОБЯЗАТЕЛЬНО: для использования custom execute_signal стратегия должна быть
# зарегистрированным execution adapter в core.strategy_loader.
# Самообъявления в модуле недостаточно.

# __execution_adapter__ = "my-registered-adapter"

# def execute_signal(signal: dict, connector, params: dict, account_id: str) -> None:
#     action = signal.get('action')
#     ...
