# core/chart_cache.py
# Кеш исторических свечей на диске (pickle).
# Используется chart_window для мгновенного открытия графика и инкрементальной догрузки.
# Структура: data/chart_cache/{board}/{ticker}/{timeframe}.pkl
# Ключ: board + ticker + timeframe.
# Pickle быстрее CSV, сохраняет типы данных (DatetimeIndex, float64, int64) без конвертации.

from pathlib import Path
from datetime import datetime
from typing import Optional
import pickle
import threading
import pandas as pd
from loguru import logger

from config.settings import DATA_DIR

CACHE_DIR = DATA_DIR / 'chart_cache'

# Per-key lock для конкурентных save/load по одному ключу
_key_locks: dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()


def _get_key_lock(key: str) -> threading.Lock:
    """Возвращает lock для конкретного cache-ключа (board/ticker/timeframe)."""
    with _key_locks_guard:
        if key not in _key_locks:
            _key_locks[key] = threading.Lock()
        return _key_locks[key]


def _safe_path_part(value: str) -> str:
    return str(value or '').replace('/', '_').replace('\\', '_').strip() or 'UNKNOWN'


def _path(ticker: str, timeframe: str, board: str = 'TQBR') -> Path:
    p = CACHE_DIR / _safe_path_part(board) / _safe_path_part(ticker)
    p.mkdir(parents=True, exist_ok=True)
    return p / f'{_safe_path_part(timeframe)}.pkl'


def _quarantine_bad_cache(path: Path, board: str, ticker: str, timeframe: str, reason: str) -> None:
    """Перемещает явно битый кеш в quarantine-файл вместо немедленного удаления."""
    try:
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        quarantine_path = path.with_suffix(path.suffix + f'.corrupt_{reason}_{stamp}')
        path.replace(quarantine_path)
        logger.warning(
            f'[Cache] Битый кеш {board}/{ticker}/{timeframe} перемещён в {quarantine_path.name}'
        )
    except Exception as e:
        logger.warning(f'[Cache] Не удалось переместить битый кеш {board}/{ticker}/{timeframe}: {e}')


def load(ticker: str, timeframe: str, board: str = 'TQBR') -> Optional[pd.DataFrame]:
    """Загружает кеш с диска. Возвращает None если кеша нет."""
    key = f"{board}/{ticker}/{timeframe}"
    lock = _get_key_lock(key)
    with lock:
        path = _path(ticker, timeframe, board)
        if not path.exists():
            return None
        try:
            with open(path, 'rb') as f:
                df = pickle.load(f)
        except (pickle.UnpicklingError, EOFError, AttributeError, ValueError) as e:
            logger.warning(f'[Cache] Битый кеш {key}: {e}')
            _quarantine_bad_cache(path, board, ticker, timeframe, 'pickle')
            return None
        except Exception as e:
            logger.warning(f'[Cache] Ошибка чтения {key}: {e}')
            return None

        if not isinstance(df, pd.DataFrame):
            logger.warning(f'[Cache] Некорректный тип кеша {key}: {type(df).__name__}')
            _quarantine_bad_cache(path, board, ticker, timeframe, 'type')
            return None

        if df.empty:
            return None

        try:
            df.index = pd.to_datetime(df.index)
        except Exception as e:
            logger.warning(f'[Cache] Некорректный индекс кеша {key}: {e}')
            _quarantine_bad_cache(path, board, ticker, timeframe, 'index')
            return None

        logger.debug(f'[Cache] Загружен {key}: {len(df)} баров, '
                     f'последний: {df.index[-1]}')
        return df


def save(ticker: str, timeframe: str, df: pd.DataFrame, board: str = 'TQBR'):
    """Сохраняет df в кеш. Per-key lock предотвращает конкурентную запись."""
    if df is None or df.empty:
        return
    key = f"{board}/{ticker}/{timeframe}"
    lock = _get_key_lock(key)
    with lock:
        temp_path = None
        try:
            path = _path(ticker, timeframe, board)
            temp_path = path.with_suffix(path.suffix + '.tmp')
            # Сохраняем только OHLCV + индикаторные колонки (_*)
            cols = [c for c in df.columns if c in ('Open', 'High', 'Low', 'Close', 'Volume')
                    or c.startswith('_')]
            with open(temp_path, 'wb') as f:
                pickle.dump(df[cols], f, protocol=pickle.HIGHEST_PROTOCOL)
            temp_path.replace(path)
            logger.debug(f"[Cache] Сохранён {key}: {len(df)} баров")
        except Exception as e:
            logger.warning(f"[Cache] Ошибка записи {key}: {e}")
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass


def merge(cached: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    """Мержит кеш с новыми барами.
    
    Логика:
    - Если fresh начинается ПОСЛЕ последнего бара кеша — просто конкатенируем
    - Если fresh ПЕРЕКРЫВАЕТСЯ с кешем — берём кеш включая последний бар + fresh,
      затем дедуплицируем с keep='last' (fresh перезаписывает кеш при конфликте)
    """
    if cached is None or cached.empty:
        return fresh
    if fresh is None or fresh.empty:
        return cached
    
    cutoff = cached.index[-1]
    
    if fresh.index[0] > cutoff:
        # Бары не пересекаются — просто добавляем fresh к кешу
        combined = pd.concat([cached, fresh])
    else:
        # Бары пересекаются — берём весь кеш (включая последний бар) + fresh
        # Дедупликация с keep='last' обеспечит приоритет fresh над кешем
        combined = pd.concat([cached, fresh])
    
    combined = combined[~combined.index.duplicated(keep="last")]
    combined.sort_index(inplace=True)
    return combined


def last_bar_time(ticker: str, timeframe: str, board: str = 'TQBR') -> Optional[datetime]:
    """Возвращает время последнего бара в кеше."""
    df = load(ticker, timeframe, board)
    if df is None or df.empty:
        return None
    return df.index[-1].to_pydatetime()


def cleanup_tmp_files():
    """Удаляет orphan .tmp файлы из chart_cache при старте."""
    if not CACHE_DIR.exists():
        return
    count = 0
    for tmp_file in CACHE_DIR.rglob("*.tmp"):
        try:
            tmp_file.unlink()
            count += 1
        except OSError as e:
            logger.warning(f"[Cache] Не удалось удалить {tmp_file.name}: {e}")
    if count:
        logger.info(f"[Cache] Удалено {count} orphan .tmp файлов")
