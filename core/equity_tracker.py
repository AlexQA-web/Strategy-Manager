# core/equity_tracker.py
"""
Трекер equity агента. Каждые 30 сек записывает текущий equity
(реализованный P/L + плавающий P/L открытой позиции) и считает
реальную максимальную просадку.

Данные хранятся персистентно в data/equity/<strategy_id>.json

Оптимизация I/O: состояние хранится in-memory в _cache.
Flush на диск происходит не чаще раза в FLUSH_INTERVAL секунд,
либо принудительно через flush_all() при остановке приложения.
"""

import json
import threading
import time
from pathlib import Path
from typing import Optional

from loguru import logger

DATA_DIR = Path(__file__).parent.parent / "data" / "equity"

FLUSH_INTERVAL = 30  # секунд между записями на диск (было 300)

# In-memory кеш: strategy_id → {"state": dict, "dirty": bool, "last_flush": float}
_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


def _ensure_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _file_path(strategy_id: str) -> Path:
    return DATA_DIR / f"{strategy_id}.json"


def _load_from_disk(strategy_id: str) -> dict:
    path = _file_path(strategy_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"peak": None, "max_drawdown": 0.0, "last_equity": 0.0, "samples": 0}


def _flush_to_disk(strategy_id: str, state: dict):
    _ensure_dir()
    path = _file_path(strategy_id)
    try:
        path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[equity_tracker] Ошибка записи {strategy_id}: {e}")


def _get_cached(strategy_id: str) -> dict:
    """Возвращает состояние из кеша, при необходимости загружает с диска."""
    with _cache_lock:
        if strategy_id not in _cache:
            state = _load_from_disk(strategy_id)
            _cache[strategy_id] = {
                "state": state,
                "dirty": False,
                "last_flush": time.monotonic(),
            }
        return _cache[strategy_id]["state"]


def record_equity(strategy_id: str, equity: float, position_qty: int = 1, force_flush: bool = False):
    """Записывает текущий equity и обновляет peak / max_drawdown.

    equity = реализованный P/L (закрытые сделки) + плавающий P/L (открытая позиция).
    position_qty = кол-во контрактов/лотов в текущей позиции (для нормализации dd к 1 лоту).
    force_flush = принудительный сброс на диск сразу (для сохранения данных после сделки).
    """
    with _cache_lock:
        if strategy_id not in _cache:
            state = _load_from_disk(strategy_id)
            _cache[strategy_id] = {
                "state": state,
                "dirty": False,
                "last_flush": time.monotonic(),
            }
        entry = _cache[strategy_id]
        state = entry["state"]

        state["last_equity"] = equity
        state["samples"] = state.get("samples", 0) + 1

        peak = state.get("peak", None)
        if peak is None or equity > peak:
            peak = equity
            state["peak"] = peak

        dd = peak - equity
        # Нормализуем dd к 1 лоту/контракту
        if position_qty != 0:
            qty = abs(position_qty)
            dd_per_lot = dd / qty
            if dd_per_lot > state.get("max_drawdown", 0.0):
                state["max_drawdown"] = round(dd_per_lot, 2)

        entry["dirty"] = True

        # Flush если прошло достаточно времени ИЛИ принудительно
        now = time.monotonic()
        if force_flush or now - entry["last_flush"] >= FLUSH_INTERVAL:
            _flush_to_disk(strategy_id, state)
            entry["dirty"] = False
            entry["last_flush"] = now


def get_max_drawdown(strategy_id: str) -> Optional[float]:
    """Возвращает реальную макс. просадку агента на 1 лот/контракт."""
    state = _get_cached(strategy_id)
    dd = state.get("max_drawdown", 0.0)
    return round(dd, 2) if dd > 0 else None


def get_equity_state(strategy_id: str) -> dict:
    """Возвращает полное состояние equity трекера."""
    return dict(_get_cached(strategy_id))


def flush_all():
    """Принудительно сбрасывает все dirty-состояния на диск.

    Вызывать при остановке приложения / агента чтобы не потерять данные.
    """
    with _cache_lock:
        for sid, entry in _cache.items():
            if entry.get("dirty"):
                _flush_to_disk(sid, entry["state"])
                entry["dirty"] = False
                entry["last_flush"] = time.monotonic()
    logger.debug("[equity_tracker] flush_all выполнен")


def reset(strategy_id: str):
    """Сбрасывает трекер (при необходимости)."""
    with _cache_lock:
        _cache.pop(strategy_id, None)
    _ensure_dir()
    path = _file_path(strategy_id)
    if path.exists():
        path.unlink()
