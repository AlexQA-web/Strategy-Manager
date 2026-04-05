"""Утилиты для работы с индикаторами и числовыми значениями."""

import math


def is_nan(v) -> bool:
    """Проверяет, является ли значение NaN или None.

    Используется в стратегиях для проверки валидности индикаторных значений.
    Заменяет дублирующиеся _nan()/_bad() в отдельных модулях.
    """
    if v is None:
        return True
    try:
        return math.isnan(v)
    except (TypeError, ValueError):
        return True
