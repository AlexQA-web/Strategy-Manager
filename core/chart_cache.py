# core/chart_cache.py
# Кеш исторических свечей на диске (pickle).
# Используется chart_window для мгновенного открытия графика и инкрементальной догрузки.
# Структура: data/chart_cache/{ticker}/{timeframe}.pkl
# Ключ: ticker + timeframe (board не нужен — тикер уникален).
# Pickle быстрее CSV, сохраняет типы данных (DatetimeIndex, float64, int64) без конвертации.

from pathlib import Path
from datetime import datetime
from typing import Optional
import pickle
import pandas as pd
from loguru import logger

CACHE_DIR = Path(__file__).parent.parent / "data" / "chart_cache"


def _path(ticker: str, timeframe: str) -> Path:
    p = CACHE_DIR / ticker
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{timeframe}.pkl"


def load(ticker: str, timeframe: str) -> Optional[pd.DataFrame]:
    """Загружает кеш с диска. Возвращает None если кеша нет."""
    path = _path(ticker, timeframe)
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            df = pickle.load(f)
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        df.index = pd.to_datetime(df.index)
        logger.debug(f"[Cache] Загружен {ticker}/{timeframe}: {len(df)} баров, "
                     f"последний: {df.index[-1]}")
        return df
    except Exception as e:
        logger.warning(f"[Cache] Ошибка чтения {ticker}/{timeframe}: {e}")
        path.unlink(missing_ok=True)
        return None


def save(ticker: str, timeframe: str, df: pd.DataFrame):
    """Сохраняет df в кеш."""
    if df is None or df.empty:
        return
    try:
        path = _path(ticker, timeframe)
        # Сохраняем только OHLCV + индикаторные колонки (_*)
        cols = [c for c in df.columns if c in ("Open", "High", "Low", "Close", "Volume")
                or c.startswith("_")]
        with open(path, "wb") as f:
            pickle.dump(df[cols], f, protocol=pickle.HIGHEST_PROTOCOL)
        logger.debug(f"[Cache] Сохранён {ticker}/{timeframe}: {len(df)} баров")
    except Exception as e:
        logger.warning(f"[Cache] Ошибка записи {ticker}/{timeframe}: {e}")


def merge(cached: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    """Мержит кеш с новыми барами. Перезаписывает последний бар кеша (мог не закрыться)."""
    if cached is None or cached.empty:
        return fresh
    if fresh is None or fresh.empty:
        return cached
    # Убираем последний бар кеша — он мог быть незакрытым
    cutoff = cached.index[-1]
    cached_trimmed = cached[cached.index < cutoff]
    combined = pd.concat([cached_trimmed, fresh])
    combined = combined[~combined.index.duplicated(keep="last")]
    combined.sort_index(inplace=True)
    return combined


def last_bar_time(ticker: str, timeframe: str) -> Optional[datetime]:
    """Возвращает время последнего бара в кеше."""
    df = load(ticker, timeframe)
    if df is None or df.empty:
        return None
    return df.index[-1].to_pydatetime()
