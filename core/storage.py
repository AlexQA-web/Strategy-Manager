# core/storage.py

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional
from loguru import logger
from config.settings import DATA_DIR

_write_lock = threading.Lock()   # защита от конкурентных записей
_cache_lock = threading.Lock()   # защита кэша
_cache: dict[str, tuple[Any, float, float]] = {}  # path → (data, monotonic_ts, mtime)
_CACHE_TTL = 2.0  # секунды


def _read(filepath: Path, use_cache: bool = True) -> Any:
    """Читает данные из файла или кэша.
    
    Args:
        filepath: Путь к файлу
        use_cache: Если False - игнорирует кэш и читает напрямую с диска.
                   Используется внутри _write_lock для избежания race condition.
    """
    key = str(filepath)
    current_mtime = filepath.stat().st_mtime if filepath.exists() else 0

    if use_cache:
        with _cache_lock:
            entry = _cache.get(key)
            if entry:
                data, cached_at, cached_mtime = entry
                # Инвалидируем если TTL истёк ИЛИ файл изменился
                if time.monotonic() - cached_at < _CACHE_TTL and current_mtime == cached_mtime:
                    return data

    if not filepath.exists() or filepath.stat().st_size == 0:
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        with _cache_lock:
            _cache[key] = (data, time.monotonic(), current_mtime)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Ошибка чтения {filepath.name}: {e}")
        # Пробуем восстановить из .bak
        bak = filepath.with_suffix(filepath.suffix + ".bak")
        if bak.exists():
            try:
                with open(bak, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.warning(f"Восстановлено из бэкапа: {bak.name}")
                with _cache_lock:
                    _cache[key] = (data, time.monotonic(), current_mtime)
                return data
            except Exception as e2:
                logger.error(f"Бэкап тоже повреждён {bak.name}: {e2}")
        return {}


def _write_unsafe(filepath: Path, data: Any):
    """Запись без захвата lock — вызывать только внутри with _write_lock.
    
    Выполняет атомарную запись через .tmp с бэкапом предыдущей версии.
    """
    try:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        # Бэкап текущего файла перед перезаписью
        if filepath.exists() and filepath.stat().st_size > 0:
            bak = filepath.with_suffix(filepath.suffix + ".bak")
            try:
                shutil.copy2(filepath, bak)
            except OSError as e:
                logger.warning(f"Не удалось создать бэкап {filepath.name}: {e}")
        tmp = filepath.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(filepath)
    except OSError as e:
        logger.error(f"Ошибка записи {filepath.name}: {e}")
        raise
    # Инвалидируем кэш
    with _cache_lock:
        _cache.pop(str(filepath), None)


def _write(filepath: Path, data: Any):
    """Атомарная запись через .tmp с бэкапом предыдущей версии.
    
    Захватывает _write_lock перед записью.
    """
    with _write_lock:
        _write_unsafe(filepath, data)


# ── Настройки приложения ──────────────────────────────────────────────────────

SETTINGS_FILE = DATA_DIR / "settings.json"

def get_settings() -> dict:
    return _read(SETTINGS_FILE)

def save_settings(data: dict):
    _write(SETTINGS_FILE, data)

def get_setting(key: str, default=None) -> Any:
    return get_settings().get(key, default)

def save_setting(key: str, value: Any):
    """Сохранить одну настройку. Потокобезопасно (read-modify-write внутри lock)."""
    with _write_lock:
        settings = _read(SETTINGS_FILE, use_cache=False)
        settings[key] = value
        _write_unsafe(SETTINGS_FILE, settings)

def get_bool_setting(key: str, default: bool = False) -> bool:
    """Безопасное чтение булевой настройки."""
    val = get_setting(key)
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).lower() == "true"

set_setting = save_setting  # alias для обратной совместимости

# ── Стратегии ────────────────────────────────────────────────────────────────

STRATEGIES_FILE = DATA_DIR / "strategies.json"

def get_all_strategies() -> dict:
    return _read(STRATEGIES_FILE)

def get_strategy(strategy_id: str) -> Optional[dict]:
    return get_all_strategies().get(strategy_id)

def save_strategy(strategy_id: str, data: dict):
    strategies = get_all_strategies()
    strategies[strategy_id] = data
    _write(STRATEGIES_FILE, strategies)

def delete_strategy(strategy_id: str) -> bool:
    strategies = get_all_strategies()
    if strategy_id not in strategies:
        return False
    del strategies[strategy_id]
    _write(STRATEGIES_FILE, strategies)
    logger.info(f"Стратегия {strategy_id} удалена")
    return True

# ── Расписания коннекторов ────────────────────────────────────────────────────

SCHEDULES_FILE = DATA_DIR / "schedules.json"

_SCHEDULES_DEFAULT = {
    "finam": {
        "connect_time": "06:50", "disconnect_time": "23:45",
        "days": [0, 1, 2, 3, 4], "is_active": True,
    },
    "quik": {
        "connect_time": "06:55", "disconnect_time": "23:40",
        "days": [0, 1, 2, 3, 4], "is_active": True,
    },
}

def get_all_schedules() -> dict:
    data = _read(SCHEDULES_FILE)
    if not isinstance(data, dict) or not data:
        _write(SCHEDULES_FILE, _SCHEDULES_DEFAULT)
        return dict(_SCHEDULES_DEFAULT)
    first_value = next(iter(data.values()), None)
    if isinstance(first_value, list):
        logger.info("[Storage] schedules.json: старый формат → сброс")
        _write(SCHEDULES_FILE, _SCHEDULES_DEFAULT)
        return dict(_SCHEDULES_DEFAULT)
    return data

# ── История сделок ────────────────────────────────────────────────────────────

TRADES_FILE = DATA_DIR / "trades_history.json"

def append_trade(trade: dict):
    """Атомарное добавление сделки через read-modify-write внутри lock.
    
    Использует _write_unsafe для избежания deadlock (lock уже захвачен).
    """
    with _write_lock:
        # Используем use_cache=False для избежания race condition:
        # читаем напрямую с диска, игнорируя кэш
        trades = _read(TRADES_FILE, use_cache=False)
        if not isinstance(trades, list):
            trades = []
        trades.append(trade)
        if len(trades) > 10_000:
            trades = trades[-10_000:]
        # Вызываем _write_unsafe (без lock) — lock уже захвачен
        _write_unsafe(TRADES_FILE, trades)

def get_trades(strategy_id: str = None, limit: int = 200) -> list:
    trades = _read(TRADES_FILE)
    if not isinstance(trades, list):
        return []
    if strategy_id:
        trades = [t for t in trades if t.get("strategy_id") == strategy_id]
    return trades[-limit:]
