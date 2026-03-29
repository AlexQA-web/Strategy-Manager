# core/storage.py

import base64
import ctypes
import json
import shutil
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from config.settings import APP_PROFILE_DIR, DATA_DIR


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ('cbData', wintypes.DWORD),
        ('pbData', ctypes.POINTER(ctypes.c_byte)),
    ]


_CRYPTPROTECT_UI_FORBIDDEN = 0x01
_SECRET_STORE_VERSION = 1

_write_lock = threading.Lock()   # защита от конкурентных записей
_cache_lock = threading.Lock()   # защита кэша
_cache: dict[str, tuple[Any, float, float]] = {}  # path → (data, monotonic_ts, mtime)
_CACHE_TTL = 2.0          # секунды — время жизни записи в кэше
_MAX_TRADES_HISTORY = 10_000  # максимальное количество сделок в trades_history.json

SENSITIVE_SETTING_KEYS = frozenset({
    'telegram_token',
    'telegram_chat_id',
    'finam_login',
    'finam_password',
    'account_aliases',
    'known_accounts',
})

SETTINGS_FILE = DATA_DIR / 'settings.json'
SECRETS_FILE = APP_PROFILE_DIR / 'secrets.local.json'
STRATEGIES_FILE = DATA_DIR / 'strategies.json'
SCHEDULES_FILE = DATA_DIR / 'schedules.json'
TRADES_FILE = DATA_DIR / 'trades_history.json'


if sys.platform.startswith('win'):
    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    _kernel32.LocalFree.restype = wintypes.HLOCAL


# ── Базовое чтение/запись JSON ─────────────────────────────────────────────────


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
                if time.monotonic() - cached_at < _CACHE_TTL and current_mtime == cached_mtime:
                    return data

    if not filepath.exists() or filepath.stat().st_size == 0:
        return {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        with _cache_lock:
            _cache[key] = (data, time.monotonic(), current_mtime)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f'Ошибка чтения {filepath.name}: {e}')
        bak = filepath.with_suffix(filepath.suffix + '.bak')
        if bak.exists():
            try:
                with open(bak, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                logger.warning(f'Восстановлено из бэкапа: {bak.name}')
                with _cache_lock:
                    _cache[key] = (data, time.monotonic(), current_mtime)
                return data
            except Exception as backup_error:
                logger.error(f'Бэкап тоже повреждён {bak.name}: {backup_error}')
        return {}


def _write_unsafe(filepath: Path, data: Any):
    """Запись без захвата lock — вызывать только внутри with _write_lock.

    Выполняет атомарную запись через .tmp с бэкапом предыдущей версии.
    """
    try:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        if filepath.exists() and filepath.stat().st_size > 0:
            bak = filepath.with_suffix(filepath.suffix + '.bak')
            try:
                shutil.copy2(filepath, bak)
            except OSError as e:
                logger.warning(f'Не удалось создать бэкап {filepath.name}: {e}')
        tmp = filepath.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(filepath)
    except OSError as e:
        logger.error(f'Ошибка записи {filepath.name}: {e}')
        raise
    with _cache_lock:
        _cache.pop(str(filepath), None)


def _write(filepath: Path, data: Any):
    """Атомарная запись через .tmp с бэкапом предыдущей версии.

    Захватывает _write_lock перед записью.
    """
    with _write_lock:
        _write_unsafe(filepath, data)


# ── Безопасное хранение секретов ───────────────────────────────────────────────


def _protect_bytes(data: bytes) -> bytes:
    if not sys.platform.startswith('win'):
        raise RuntimeError('Шифрование секретов поддерживается только на Windows через DPAPI')
    if not data:
        return b''

    buffer = ctypes.create_string_buffer(data, len(data))
    in_blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    out_blob = DATA_BLOB()

    ok = _crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), 'Не удалось зашифровать секрет через DPAPI')

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            _kernel32.LocalFree(out_blob.pbData)


def _unprotect_bytes(data: bytes) -> bytes:
    if not sys.platform.startswith('win'):
        raise RuntimeError('Расшифровка секретов поддерживается только на Windows через DPAPI')
    if not data:
        return b''

    buffer = ctypes.create_string_buffer(data, len(data))
    in_blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    out_blob = DATA_BLOB()
    description = wintypes.LPWSTR()

    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        ctypes.byref(description),
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), 'Не удалось расшифровать секрет через DPAPI')

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            _kernel32.LocalFree(out_blob.pbData)
        if description:
            _kernel32.LocalFree(description)


def _is_empty_secret_value(value: Any) -> bool:
    return value in (None, '', {}, [])


def _split_settings(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    public_settings: dict[str, Any] = {}
    secret_settings: dict[str, Any] = {}
    for key, value in data.items():
        if key in SENSITIVE_SETTING_KEYS:
            secret_settings[key] = value
        else:
            public_settings[key] = value
    return public_settings, secret_settings


def _encrypt_secret_value(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False).encode('utf-8')
    encrypted = _protect_bytes(payload)
    return base64.b64encode(encrypted).decode('ascii')


def _decrypt_secret_value(payload: str) -> Any:
    encrypted = base64.b64decode(payload.encode('ascii'))
    decrypted = _unprotect_bytes(encrypted)
    return json.loads(decrypted.decode('utf-8'))


def _read_secret_settings(use_cache: bool = True) -> dict[str, Any]:
    raw_data = _read(SECRETS_FILE, use_cache=use_cache)
    if not isinstance(raw_data, dict):
        return {}

    encrypted_values = raw_data.get('values', {})
    if not isinstance(encrypted_values, dict):
        return {}

    secrets: dict[str, Any] = {}
    for key, encrypted_value in encrypted_values.items():
        if key not in SENSITIVE_SETTING_KEYS or not isinstance(encrypted_value, str):
            continue
        try:
            secrets[key] = _decrypt_secret_value(encrypted_value)
        except Exception as e:
            logger.error(f'Не удалось расшифровать секрет {key}: {e}')
    return secrets


def _write_secret_settings_unsafe(data: dict[str, Any]):
    payload: dict[str, Any] = {
        'version': _SECRET_STORE_VERSION,
        'values': {},
    }
    for key, value in data.items():
        if key not in SENSITIVE_SETTING_KEYS or _is_empty_secret_value(value):
            continue
        payload['values'][key] = _encrypt_secret_value(value)
    _write_unsafe(SECRETS_FILE, payload)


# ── Публичный низкоуровневый API для других модулей core/ ──────────────────────


def read_json(filepath: Path) -> Any:
    """Публичное чтение произвольного JSON-файла через кэш."""
    return _read(filepath)


def write_json(filepath: Path, data: Any):
    """Публичная атомарная запись произвольного JSON-файла."""
    _write(filepath, data)


# ── Настройки приложения ───────────────────────────────────────────────────────


def _migrate_sensitive_settings_if_needed():
    with _write_lock:
        settings = _read(SETTINGS_FILE, use_cache=False)
        if not isinstance(settings, dict) or not settings:
            return

        migrated: dict[str, Any] = {}
        for key in SENSITIVE_SETTING_KEYS:
            if key in settings and not _is_empty_secret_value(settings[key]):
                migrated[key] = settings.pop(key)

        if not migrated:
            return

        secrets = _read_secret_settings(use_cache=False)
        secrets.update(migrated)
        _write_unsafe(SETTINGS_FILE, settings)
        _write_secret_settings_unsafe(secrets)
        logger.warning(f'Чувствительные настройки перенесены из {SETTINGS_FILE.name} в {SECRETS_FILE.name}')


def get_public_settings() -> dict:
    _migrate_sensitive_settings_if_needed()
    data = _read(SETTINGS_FILE)
    return data if isinstance(data, dict) else {}


def get_exportable_settings() -> dict:
    """Возвращает настройки без секретов для экспорта и шаблонов."""
    return dict(get_public_settings())


def get_settings() -> dict:
    settings = get_public_settings()
    settings.update(_read_secret_settings())
    return settings


def save_settings(data: dict):
    _migrate_sensitive_settings_if_needed()
    public_settings, secret_updates = _split_settings(data)
    with _write_lock:
        _write_unsafe(SETTINGS_FILE, public_settings)
        current_secrets = _read_secret_settings(use_cache=False)
        for key, value in secret_updates.items():
            if _is_empty_secret_value(value):
                current_secrets.pop(key, None)
            else:
                current_secrets[key] = value
        _write_secret_settings_unsafe(current_secrets)


def get_setting(key: str, default=None) -> Any:
    return get_settings().get(key, default)


def save_setting(key: str, value: Any):
    """Сохранить одну настройку. Потокобезопасно (read-modify-write внутри lock)."""
    _migrate_sensitive_settings_if_needed()
    with _write_lock:
        if key in SENSITIVE_SETTING_KEYS:
            secrets = _read_secret_settings(use_cache=False)
            if _is_empty_secret_value(value):
                secrets.pop(key, None)
            else:
                secrets[key] = value
            _write_secret_settings_unsafe(secrets)
            return

        settings = _read(SETTINGS_FILE, use_cache=False)
        if not isinstance(settings, dict):
            settings = {}
        settings[key] = value
        _write_unsafe(SETTINGS_FILE, settings)


def get_bool_setting(key: str, default: bool = False) -> bool:
    """Безопасное чтение булевой настройки."""
    val = get_setting(key)
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).lower() == 'true'


set_setting = save_setting  # alias для обратной совместимости


# ── Стратегии ──────────────────────────────────────────────────────────────────


def get_all_strategies() -> dict:
    return _read(STRATEGIES_FILE)


def get_strategy(strategy_id: str) -> Optional[dict]:
    return get_all_strategies().get(strategy_id)


def save_strategy(strategy_id: str, data: dict):
    with _write_lock:
        strategies = _read(STRATEGIES_FILE, use_cache=False)
        if not isinstance(strategies, dict):
            strategies = {}
        strategies[strategy_id] = data
        _write_unsafe(STRATEGIES_FILE, strategies)


def delete_strategy(strategy_id: str) -> bool:
    with _write_lock:
        strategies = _read(STRATEGIES_FILE, use_cache=False)
        if not isinstance(strategies, dict) or strategy_id not in strategies:
            return False
        del strategies[strategy_id]
        _write_unsafe(STRATEGIES_FILE, strategies)
        logger.info(f'Стратегия {strategy_id} удалена')
        return True


# ── Расписания коннекторов ─────────────────────────────────────────────────────

_SCHEDULES_DEFAULT = {
    'finam': {
        'connect_time': '06:50', 'disconnect_time': '23:45',
        'days': [0, 1, 2, 3, 4], 'is_active': True,
    },
    'quik': {
        'connect_time': '06:55', 'disconnect_time': '23:40',
        'days': [0, 1, 2, 3, 4], 'is_active': True,
    },
}


def get_all_schedules() -> dict:
    data = _read(SCHEDULES_FILE)
    if not isinstance(data, dict) or not data:
        _write(SCHEDULES_FILE, _SCHEDULES_DEFAULT)
        return dict(_SCHEDULES_DEFAULT)
    first_value = next(iter(data.values()), None)
    if isinstance(first_value, list):
        logger.info('[Storage] schedules.json: старый формат → сброс')
        _write(SCHEDULES_FILE, _SCHEDULES_DEFAULT)
        return dict(_SCHEDULES_DEFAULT)
    return data


# ── История сделок ─────────────────────────────────────────────────────────────


def append_trade(trade: dict):
    """Атомарное добавление сделки через read-modify-write внутри lock.

    Использует _write_unsafe для избежания deadlock (lock уже захвачен).
    """
    with _write_lock:
        trades = _read(TRADES_FILE, use_cache=False)
        if not isinstance(trades, list):
            trades = []
        trades.append(trade)
        if len(trades) > _MAX_TRADES_HISTORY:
            trades = trades[-_MAX_TRADES_HISTORY:]
        _write_unsafe(TRADES_FILE, trades)


def get_trades(strategy_id: str = None, limit: int = 200) -> list:
    trades = _read(TRADES_FILE)
    if not isinstance(trades, list):
        return []
    if strategy_id:
        trades = [t for t in trades if t.get('strategy_id') == strategy_id]
    return trades[-limit:]
