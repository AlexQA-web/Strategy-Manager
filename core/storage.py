# core/storage.py

import base64
import ctypes
import json
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

from config.settings import APP_PROFILE_DIR, DATA_DIR

from core.money import to_storage_float, to_storage_str
from core.rwlock import RWLock


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ('cbData', wintypes.DWORD),
        ('pbData', ctypes.POINTER(ctypes.c_byte)),
    ]


_CRYPTPROTECT_UI_FORBIDDEN = 0x01
_SECRET_STORE_VERSION = 1
_MAX_BACKUPS = 3  # максимальное количество бэкап-файлов
_RUNTIME_SCHEMA_VERSION = 1
_RUNTIME_SCHEMA_VERSION_KEY = 'schema_version'
_RUNTIME_SCHEMA_PAYLOAD_KEY = 'payload'

_rwlock = RWLock()           # единый readers-writer lock
_cache: dict[str, tuple[Any, float, float, bool]] = {}  # path → (data, monotonic_ts, mtime, needs_persist)
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
PENDING_ORDERS_FILE = DATA_DIR / 'pending_orders.json'
TRADES_ARCHIVE_DIR_NAME = 'trades_archive'


@dataclass(frozen=True)
class RuntimeJsonSchema:
    current_version: int
    default_factory: Callable[[], Any]
    migrations: dict[int, Callable[[Any], Any]]


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


def _default_dict_payload() -> dict:
    return {}


def _default_list_payload() -> list:
    return []


def _default_schedules_payload() -> dict:
    return dict(_SCHEDULES_DEFAULT)


def _migrate_dict_payload(payload: Any) -> dict:
    return payload if isinstance(payload, dict) else {}


def _migrate_list_payload(payload: Any) -> list:
    return payload if isinstance(payload, list) else []


def _migrate_schedules_payload(payload: Any) -> dict:
    if not isinstance(payload, dict) or not payload:
        return _default_schedules_payload()
    first_value = next(iter(payload.values()), None)
    if isinstance(first_value, list):
        logger.info('[Storage] schedules.json: старый формат → сброс')
        return _default_schedules_payload()
    return payload


_RUNTIME_JSON_SCHEMAS: dict[str, RuntimeJsonSchema] = {
    'settings.json': RuntimeJsonSchema(_RUNTIME_SCHEMA_VERSION, _default_dict_payload, {0: _migrate_dict_payload}),
    'strategies.json': RuntimeJsonSchema(_RUNTIME_SCHEMA_VERSION, _default_dict_payload, {0: _migrate_dict_payload}),
    'schedules.json': RuntimeJsonSchema(_RUNTIME_SCHEMA_VERSION, _default_schedules_payload, {0: _migrate_schedules_payload}),
    'trades_history.json': RuntimeJsonSchema(_RUNTIME_SCHEMA_VERSION, _default_list_payload, {0: _migrate_list_payload}),
    'pending_orders.json': RuntimeJsonSchema(_RUNTIME_SCHEMA_VERSION, _default_dict_payload, {0: _migrate_dict_payload}),
    'order_history.json': RuntimeJsonSchema(_RUNTIME_SCHEMA_VERSION, _default_dict_payload, {0: _migrate_dict_payload}),
}


def _get_runtime_json_schema(filepath: Path) -> Optional[RuntimeJsonSchema]:
    return _RUNTIME_JSON_SCHEMAS.get(filepath.name)


def _wrap_runtime_json_payload(filepath: Path, data: Any) -> Any:
    schema = _get_runtime_json_schema(filepath)
    if schema is None:
        return data
    return {
        _RUNTIME_SCHEMA_VERSION_KEY: schema.current_version,
        _RUNTIME_SCHEMA_PAYLOAD_KEY: data,
    }


def _decode_runtime_json_payload(filepath: Path, data: Any) -> tuple[Any, bool]:
    schema = _get_runtime_json_schema(filepath)
    if schema is None:
        return data, False

    payload = data
    version = 0
    enveloped = False
    if isinstance(data, dict) and _RUNTIME_SCHEMA_VERSION_KEY in data and _RUNTIME_SCHEMA_PAYLOAD_KEY in data:
        enveloped = True
        payload = data.get(_RUNTIME_SCHEMA_PAYLOAD_KEY)
        try:
            version = int(data.get(_RUNTIME_SCHEMA_VERSION_KEY, 0) or 0)
        except (TypeError, ValueError):
            version = 0

    if version > schema.current_version:
        logger.warning(
            f'[Storage] {filepath.name}: unsupported schema_version={version}, '
            f'expected <= {schema.current_version}'
        )
        return payload, False

    migrated = not enveloped
    while version < schema.current_version:
        migration = schema.migrations.get(version)
        if migration is None:
            logger.warning(
                f'[Storage] {filepath.name}: missing migration {version} -> {version + 1}, '
                f'falling back to default payload'
            )
            payload = schema.default_factory()
            migrated = True
            version = schema.current_version
            break
        payload = migration(payload)
        version += 1
        migrated = True

    return payload, migrated


def _read_with_metadata(filepath: Path, use_cache: bool = True) -> tuple[Any, bool]:
    key = str(filepath)
    current_mtime = filepath.stat().st_mtime if filepath.exists() else 0

    if use_cache:
        with _rwlock.read_lock():
            entry = _cache.get(key)
            if entry:
                data, cached_at, cached_mtime, needs_persist = entry
                if time.monotonic() - cached_at < _CACHE_TTL and current_mtime == cached_mtime:
                    return data, needs_persist

    file_missing = not filepath.exists() or filepath.stat().st_size == 0
    if file_missing:
        schema = _get_runtime_json_schema(filepath)
        default_payload = schema.default_factory() if schema is not None else {}
        return default_payload, False

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            raw_data = json.load(f)
        data, needs_persist = _decode_runtime_json_payload(filepath, raw_data)
        _cache[key] = (data, time.monotonic(), current_mtime, needs_persist)
        return data, needs_persist
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f'Ошибка чтения {filepath.name}: {e}')
        for i in range(1, _MAX_BACKUPS + 1):
            backup_data = _read_backup(filepath, i)
            if backup_data is not None:
                decoded_backup, backup_needs_persist = _decode_runtime_json_payload(filepath, backup_data)
                logger.warning(f'Восстановлено из бэкапа: {filepath.with_suffix("").name}.bak{i}{filepath.suffix}')
                _cache[key] = (decoded_backup, time.monotonic(), current_mtime, True or backup_needs_persist)
                return decoded_backup, True
        schema = _get_runtime_json_schema(filepath)
        default_payload = schema.default_factory() if schema is not None else {}
        return default_payload, False


def cleanup_orphan_tmp(directory: Path = None):
    """Удаляет orphan .tmp файлы, оставшиеся после аварийного завершения.

    Вызывается при старте приложения. Если .tmp существует, а основной файл
    повреждён или отсутствует, пытается восстановить из .tmp.
    """
    dirs = [directory] if directory else [DATA_DIR, APP_PROFILE_DIR]
    for d in dirs:
        if not d.exists():
            continue
        for tmp_file in d.glob("*.tmp"):
            try:
                # Определяем основной файл
                main_file = tmp_file.with_suffix("")
                if not main_file.suffix:
                    # .json.tmp → .json
                    main_file = tmp_file.with_name(tmp_file.stem)

                if main_file.exists() and main_file.stat().st_size > 0:
                    # Основной файл в порядке — orphan tmp не нужен
                    tmp_file.unlink()
                    logger.debug(f"Удалён orphan tmp: {tmp_file.name}")
                else:
                    # Основной файл повреждён/отсутствует — пробуем восстановить из tmp
                    try:
                        with open(tmp_file, "r", encoding="utf-8") as f:
                            json.load(f)  # валидация JSON
                        tmp_file.replace(main_file)
                        logger.warning(f"Восстановлен из orphan tmp: {tmp_file.name} → {main_file.name}")
                    except (json.JSONDecodeError, OSError):
                        tmp_file.unlink()
                        logger.warning(f"Удалён невалидный orphan tmp: {tmp_file.name}")
            except OSError as e:
                logger.warning(f"cleanup_orphan_tmp error: {tmp_file.name}: {e}")


def _read(filepath: Path, use_cache: bool = True) -> Any:
    """Читает данные из файла или кэша.

    Args:
        filepath: Путь к файлу
        use_cache: Если False - игнорирует кэш и читает напрямую с диска.
                   Используется внутри write-блокировки для избежания stale reads.
    """
    data, _ = _read_with_metadata(filepath, use_cache=use_cache)
    return data


def _cleanup_old_backups(filepath: Path):
    """Удаляет старые бэкап-файлы, оставляя не более _MAX_BACKUPS."""
    suffix = filepath.suffix
    base = filepath.with_suffix('')
    # Удаляем .bakN где N > _MAX_BACKUPS
    for i in range(_MAX_BACKUPS + 1, _MAX_BACKUPS + 10):
        old_bak = Path(f'{base}.bak{i}{suffix}')
        if old_bak.exists():
            try:
                old_bak.unlink()
                logger.debug(f'Удалён старый бэкап: {old_bak.name}')
            except OSError as e:
                logger.warning(f'Не удалось удалить {old_bak.name}: {e}')


def _rotate_backups(filepath: Path):
    """Ротация бэкапов: .bak → .bak2, текущий → .bak, новый записывается.
    
    Атомарное копирование: сначала во временный файл, потом rename.
    Цепочка: .bak (последний) → .bak2 → .bak3
    
    Каждый шаг обёрнут в try/except — частичная ротация допустима,
    запись основного файла не должна прерываться из-за ошибки в бэкапах.
    """
    suffix = filepath.suffix
    base = filepath.with_suffix('')
    
    # Сдвигаем существующие бэкапы: .bak(N-1) → .bak(N)
    # .bak2 → .bak3
    for i in range(_MAX_BACKUPS, 2, -1):
        src = Path(f'{base}.bak{i - 1}{suffix}')
        dst = Path(f'{base}.bak{i}{suffix}')
        if src.exists():
            try:
                src.rename(dst)
            except OSError as e:
                logger.debug(f'Не удалось переименовать {src.name} → {dst.name}: {e}')
    
    # .bak → .bak2
    bak1 = Path(f'{base}.bak{suffix}')
    bak2 = Path(f'{base}.bak2{suffix}')
    if bak1.exists():
        try:
            bak1.rename(bak2)
        except OSError as e:
            logger.debug(f'Не удалось переименовать {bak1.name} → {bak2.name}: {e}')
    
    # Текущий файл → .bak (атомарно через .tmp → rename)
    if filepath.exists() and filepath.stat().st_size > 0:
        bak = Path(f'{base}.bak{suffix}')
        bak_tmp = Path(f'{base}.bak{suffix}.tmp')
        try:
            shutil.copy2(filepath, bak_tmp)
            bak_tmp.replace(bak)
        except OSError as e:
            logger.warning(f'Не удалось создать бэкап {filepath.name}: {e}')
            try:
                bak_tmp.unlink(missing_ok=True)
            except OSError:
                pass
    
    # Очищаем старые бэкапы
    try:
        _cleanup_old_backups(filepath)
    except Exception as e:
        logger.debug(f'cleanup_old_backups error: {e}')


def _read_backup(filepath: Path, n: int = 1) -> Optional[Any]:
    """Читает конкретный бэкап-файл (.bakN).
    
    Args:
        filepath: Путь к основному файлу
        n: Номер бэкапа (1 = .bak, 2 = .bak2, и т.д.)
    
    Returns:
        Данные из бэкапа или None если файл не существует
    """
    suffix = filepath.suffix
    base = filepath.with_suffix('')
    if n == 1:
        bak_path = Path(f'{base}.bak{suffix}')
    else:
        bak_path = Path(f'{base}.bak{n}{suffix}')
    
    if not bak_path.exists():
        return None
    
    try:
        with open(bak_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f'Ошибка чтения бэкапа {bak_path.name}: {e}')
        return None


def _write_unsafe_inner(filepath: Path, data: Any):
    """Запись без захвата lock — вызывать только внутри write-блокировки.

    Протокол: prepare → write tmp → flush → commit (rename) → cleanup.
    При ошибке orphan .tmp удаляется.
    """
    tmp = filepath.with_suffix('.tmp')
    payload = _wrap_runtime_json_payload(filepath, data)
    try:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        # Ротация бэкапов вместо простого копирования
        if filepath.exists() and filepath.stat().st_size > 0:
            _rotate_backups(filepath)
        # Prepare + write
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
        # Commit
        tmp.replace(filepath)
    except (OSError, TypeError, ValueError) as e:
        logger.error(f'Ошибка записи {filepath.name}: {e}')
        # Cleanup orphan tmp
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    # Invalidation кэша происходит внутри write-блокировки вызывающего
    _cache.pop(str(filepath), None)


def _write_unsafe(filepath: Path, data: Any):
    """Запись с захватом write-lock.

    Атомарная запись через .tmp с ротацией бэкапов и инвалидацией кэша.
    """
    with _rwlock.write_lock():
        _write_unsafe_inner(filepath, data)


def _write(filepath: Path, data: Any):
    """Атомарная запись через .tmp с бэкапом предыдущей версии.

    Захватывает _rwlock.write_lock() перед записью.
    """
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
    """Вызывать только внутри write-блокировки."""
    payload: dict[str, Any] = {
        'version': _SECRET_STORE_VERSION,
        'values': {},
    }
    for key, value in data.items():
        if key not in SENSITIVE_SETTING_KEYS or _is_empty_secret_value(value):
            continue
        payload['values'][key] = _encrypt_secret_value(value)
    _write_unsafe_inner(SECRETS_FILE, payload)


# ── Публичный низкоуровневый API для других модулей core/ ──────────────────────


def read_json(filepath: Path) -> Any:
    """Публичное чтение произвольного JSON-файла через кэш."""
    data, needs_persist = _read_with_metadata(filepath)
    if needs_persist and filepath.exists() and filepath.stat().st_size > 0:
        _write(filepath, data)
    return data


def write_json(filepath: Path, data: Any):
    """Публичная атомарная запись произвольного JSON-файла."""
    _write(filepath, data)


# ── Настройки приложения ───────────────────────────────────────────────────────


def _migrate_sensitive_settings_if_needed():
    with _rwlock.write_lock():
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
        _write_unsafe_inner(SETTINGS_FILE, settings)
        _write_secret_settings_unsafe(secrets)
        logger.warning(f'Чувствительные настройки перенесены из {SETTINGS_FILE.name} в {SECRETS_FILE.name}')


def get_public_settings() -> dict:
    _migrate_sensitive_settings_if_needed()
    data = read_json(SETTINGS_FILE)
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
    with _rwlock.write_lock():
        _write_unsafe_inner(SETTINGS_FILE, public_settings)
        current_secrets = _read_secret_settings(use_cache=False)
        for key, value in secret_updates.items():
            if _is_empty_secret_value(value):
                current_secrets.pop(key, None)
            else:
                current_secrets[key] = value
        _write_secret_settings_unsafe(current_secrets)


def get_setting(key: str, default=None) -> Any:
    return get_settings().get(key, default)


def get_bool_setting(key: str, default: bool = False) -> bool:
    """Безопасное получение boolean-настройки из строкового значения."""
    val = get_setting(key)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes", "on")
    if isinstance(val, (int, float)):
        return bool(val)
    return default


def save_setting(key: str, value: Any):
    """Сохранить одну настройку. Потокобезопасно (read-modify-write внутри lock)."""
    _migrate_sensitive_settings_if_needed()
    with _rwlock.write_lock():
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
        _write_unsafe_inner(SETTINGS_FILE, settings)


set_setting = save_setting  # alias для обратной совместимости


# ── Стратегии ──────────────────────────────────────────────────────────────────


def get_all_strategies() -> dict:
    data = read_json(STRATEGIES_FILE)
    return data if isinstance(data, dict) else {}


def get_strategy(strategy_id: str) -> Optional[dict]:
    return get_all_strategies().get(strategy_id)


def save_strategy(strategy_id: str, data: dict):
    with _rwlock.write_lock():
        strategies = _read(STRATEGIES_FILE, use_cache=False)
        if not isinstance(strategies, dict):
            strategies = {}
        strategies[strategy_id] = data
        _write_unsafe_inner(STRATEGIES_FILE, strategies)


def delete_strategy(strategy_id: str) -> bool:
    with _rwlock.write_lock():
        strategies = _read(STRATEGIES_FILE, use_cache=False)
        if not isinstance(strategies, dict) or strategy_id not in strategies:
            return False
        del strategies[strategy_id]
        _write_unsafe_inner(STRATEGIES_FILE, strategies)
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
    data = read_json(SCHEDULES_FILE)
    if not isinstance(data, dict) or not data:
        _write(SCHEDULES_FILE, _SCHEDULES_DEFAULT)
        return dict(_SCHEDULES_DEFAULT)
    return data


def _get_trades_archive_dir(filepath: Path) -> Path:
    return filepath.parent / TRADES_ARCHIVE_DIR_NAME


def _archive_trades_snapshot(filepath: Path, trades: list[dict]):
    if not trades:
        return
    archive_dir = _get_trades_archive_dir(filepath)
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime('%Y%m%d_%H%M%S')
    archive_file = archive_dir / f'{filepath.stem}_{ts}_{time.time_ns() % 1_000_000}.json'
    archive_payload = {
        'archived_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'source_file': filepath.name,
        'trades': list(trades),
    }
    _write_unsafe_inner(archive_file, archive_payload)


# ── История сделок ─────────────────────────────────────────────────────────────


def append_trade(trade: dict):
    """Атомарное добавление сделки через read-modify-write внутри lock.

    Использует _write_unsafe_inner для избежания deadlock (lock уже захвачен).
    """
    with _rwlock.write_lock():
        trades = _read(TRADES_FILE, use_cache=False)
        if not isinstance(trades, list):
            trades = []
        normalized_trade = dict(trade)
        for key in ('price', 'commission', 'pnl'):
            if key in normalized_trade and normalized_trade[key] is not None:
                normalized_trade[key] = to_storage_float(normalized_trade[key])
                normalized_trade[f'{key}_decimal'] = to_storage_str(normalized_trade[key])
        execution_id = str(trade.get('execution_id', '') or '')
        if execution_id:
            for existing in trades:
                if str(existing.get('execution_id', '') or '') == execution_id:
                    return 'duplicate'
        trades.append(normalized_trade)
        if len(trades) > _MAX_TRADES_HISTORY:
            overflow_count = len(trades) - _MAX_TRADES_HISTORY
            archive_count = max(overflow_count, max(1, _MAX_TRADES_HISTORY // 10))
            archived_trades = trades[:archive_count]
            _archive_trades_snapshot(TRADES_FILE, archived_trades)
            trades = trades[archive_count:]
        _write_unsafe_inner(TRADES_FILE, trades)
        return 'inserted'


def get_all_pending_orders() -> dict:
    data = read_json(PENDING_ORDERS_FILE)
    return data if isinstance(data, dict) else {}


def save_pending_order(strategy_id: str, tid: str, lifecycle_data: dict):
    with _rwlock.write_lock():
        data = _read(PENDING_ORDERS_FILE, use_cache=False)
        if not isinstance(data, dict):
            data = {}
        strategy_orders = data.setdefault(strategy_id, {})
        status = 'inserted' if tid not in strategy_orders else 'updated'
        strategy_orders[tid] = lifecycle_data
        _write_unsafe_inner(PENDING_ORDERS_FILE, data)
        return status


def delete_pending_order(strategy_id: str, tid: str):
    with _rwlock.write_lock():
        data = _read(PENDING_ORDERS_FILE, use_cache=False)
        if not isinstance(data, dict):
            return False
        strategy_orders = data.get(strategy_id)
        if not isinstance(strategy_orders, dict) or tid not in strategy_orders:
            return False
        del strategy_orders[tid]
        if not strategy_orders:
            data.pop(strategy_id, None)
        _write_unsafe_inner(PENDING_ORDERS_FILE, data)
        return True


def clear_pending_orders(strategy_id: str | None = None):
    with _rwlock.write_lock():
        data = _read(PENDING_ORDERS_FILE, use_cache=False)
        if not isinstance(data, dict):
            data = {}
        if strategy_id is None:
            data = {}
        else:
            data.pop(strategy_id, None)
        _write_unsafe_inner(PENDING_ORDERS_FILE, data)


def get_trades(strategy_id: str = None, limit: int = 200) -> list:
    trades = read_json(TRADES_FILE)
    if not isinstance(trades, list):
        return []
    if strategy_id:
        trades = [t for t in trades if t.get('strategy_id') == strategy_id]
    return trades[-limit:]
