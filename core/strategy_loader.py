# core/strategy_loader.py

import importlib.util
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from enum import Enum
from pathlib import Path
from typing import Optional
from loguru import logger

REQUIRED_FUNCTIONS = ["get_info", "get_params", "on_start", "on_stop", "on_tick"]

CUSTOM_EXECUTION_ADAPTERS = {
    "achilles-basket": {
        "allowed_files": {"achilles"},
        "allowed_actions": frozenset({"snapshot", "signal", "close_limit", "close_market"}),
    },
}

_TIMEOUT_START_STOP = 30  # секунд
_CIRCUIT_BREAKER_THRESHOLD = 5  # последовательных ошибок до перехода в ERROR


class StrategyState(Enum):
    LOADED = "loaded"
    RUNNING = "running"
    ERROR = "error"
    STOPPING = "stopping"


class StrategyLoadError(Exception):
    pass


def resolve_custom_execution_adapter(module, file_path: str) -> tuple[Optional[str], frozenset[str]]:
    """Разрешает custom execution adapter только через явный registry.

    Самообъявление стратегии недостаточно: adapter должен существовать в
    CUSTOM_EXECUTION_ADAPTERS и быть разрешён для конкретного файла стратегии.
    """
    adapter_name = getattr(module, "__execution_adapter__", None)
    if not adapter_name:
        return None, frozenset()

    adapter_meta = CUSTOM_EXECUTION_ADAPTERS.get(adapter_name)
    if not adapter_meta:
        logger.error(
            f"[StrategyLoader] Неизвестный custom execution adapter: {adapter_name!r}. "
            f"Стратегия будет работать только через стандартный execution path."
        )
        return None, frozenset()

    file_stem = Path(file_path).stem.lower()
    allowed_files = {name.lower() for name in adapter_meta.get("allowed_files", set())}
    if allowed_files and file_stem not in allowed_files:
        logger.error(
            f"[StrategyLoader] Adapter {adapter_name!r} не разрешён для файла {file_stem!r}. "
            f"Стратегия будет работать только через стандартный execution path."
        )
        return None, frozenset()

    return adapter_name, frozenset(adapter_meta.get("allowed_actions", ()))


class LoadedStrategy:
    def __init__(self, strategy_id: str, module, file_path: str):
        self.strategy_id = strategy_id
        self.module = module
        self.file_path = file_path
        self.info: dict = module.get_info()
        self.params_schema: dict = module.get_params()
        self.custom_execution_adapter, self.custom_execution_actions = (
            resolve_custom_execution_adapter(module, file_path)
        )
        self._lock = threading.Lock()
        self.state = StrategyState.LOADED
        self._consecutive_errors = 0

    def call_on_start(self, params: dict, connector) -> bool:
        validation_error = validate_params(params, self.params_schema)
        if validation_error:
            logger.error(f"[{self.strategy_id}] Валидация параметров: {validation_error}")
            return False

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self._run_on_start, params, connector)
                future.result(timeout=_TIMEOUT_START_STOP)
            self.state = StrategyState.RUNNING
            self._consecutive_errors = 0
            logger.info(f"[{self.strategy_id}] on_start выполнен успешно")
            return True
        except FuturesTimeoutError:
            logger.error(f"[{self.strategy_id}] on_start: таймаут ({_TIMEOUT_START_STOP}с)")
            self.state = StrategyState.ERROR
            return False
        except Exception as e:
            logger.error(f"[{self.strategy_id}] Ошибка в on_start: {e}\n{traceback.format_exc()}")
            self.state = StrategyState.ERROR
            return False

    def _run_on_start(self, params, connector):
        with self._lock:
            self.module.on_start(params, connector)

    def call_on_stop(self, params: dict, connector) -> bool:
        self.state = StrategyState.STOPPING
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self._run_on_stop, params, connector)
                future.result(timeout=_TIMEOUT_START_STOP)
            self.state = StrategyState.LOADED
            logger.info(f"[{self.strategy_id}] on_stop выполнен успешно")
            return True
        except FuturesTimeoutError:
            logger.error(f"[{self.strategy_id}] on_stop: таймаут ({_TIMEOUT_START_STOP}с)")
            self.state = StrategyState.ERROR
            return False
        except Exception as e:
            logger.error(f"[{self.strategy_id}] Ошибка в on_stop: {e}\n{traceback.format_exc()}")
            self.state = StrategyState.ERROR
            return False

    def _run_on_stop(self, params, connector):
        with self._lock:
            self.module.on_stop(params, connector)

    def call_on_tick(self, tick_data: dict, params: dict, connector) -> bool:
        if self.state == StrategyState.ERROR:
            return False

        try:
            with self._lock:
                self.module.on_tick(tick_data, params, connector)
            self._consecutive_errors = 0
            return True
        except Exception as e:
            self._consecutive_errors += 1
            tb = traceback.format_exc()
            logger.error(
                f"[{self.strategy_id}] Ошибка в on_tick "
                f"({self._consecutive_errors}/{_CIRCUIT_BREAKER_THRESHOLD}): {e}\n{tb}"
            )

            if self._consecutive_errors >= _CIRCUIT_BREAKER_THRESHOLD:
                self.state = StrategyState.ERROR
                logger.error(
                    f"[{self.strategy_id}] Circuit breaker: "
                    f"{_CIRCUIT_BREAKER_THRESHOLD} ошибок подряд → состояние ERROR"
                )
                try:
                    from core.telegram_bot import notifier, EventCode
                    notifier.send(
                        EventCode.STRATEGY_ERROR,
                        agent=self.strategy_id,
                        description=f"Circuit breaker: {_CIRCUIT_BREAKER_THRESHOLD} ошибок подряд. "
                                    f"Последняя: {e}",
                    )
                except Exception:
                    pass
            else:
                try:
                    from core.telegram_bot import notifier, EventCode
                    notifier.send(
                        EventCode.STRATEGY_ERROR,
                        agent=self.strategy_id,
                        description=str(e),
                    )
                except Exception:
                    pass
            return False

    def call_on_bar(self, bars: list[dict], position: int, params: dict) -> dict:
        """Вызывает on_bar с circuit breaker. Возвращает {"action": ...} или {"action": None}."""
        if self.state == StrategyState.ERROR:
            return {"action": None}
        if not hasattr(self.module, "on_bar"):
            return {"action": None}

        try:
            with self._lock:
                result = self.module.on_bar(bars, position, params)
            self._consecutive_errors = 0
            return result if isinstance(result, dict) else {"action": None}
        except Exception as e:
            self._consecutive_errors += 1
            tb = traceback.format_exc()
            logger.error(
                f"[{self.strategy_id}] Ошибка в on_bar "
                f"({self._consecutive_errors}/{_CIRCUIT_BREAKER_THRESHOLD}): {e}\n{tb}"
            )
            if self._consecutive_errors >= _CIRCUIT_BREAKER_THRESHOLD:
                self.state = StrategyState.ERROR
                logger.error(
                    f"[{self.strategy_id}] Circuit breaker: "
                    f"{_CIRCUIT_BREAKER_THRESHOLD} ошибок подряд → состояние ERROR"
                )
                try:
                    from core.telegram_bot import notifier, EventCode
                    notifier.send(
                        EventCode.STRATEGY_ERROR,
                        agent=self.strategy_id,
                        description=f"Circuit breaker on_bar: {_CIRCUIT_BREAKER_THRESHOLD} ошибок. "
                                    f"Последняя: {e}",
                    )
                except Exception:
                    pass
            return {"action": None}

    def reset_error(self):
        """Ручной сброс circuit breaker."""
        self._consecutive_errors = 0
        self.state = StrategyState.LOADED
        logger.info(f"[{self.strategy_id}] Circuit breaker сброшен")

    def __repr__(self):
        return f"<LoadedStrategy id={self.strategy_id} state={self.state.value} file={self.file_path}>"


def validate_params(params: dict, schema: dict) -> Optional[str]:
    """
    Проверяет params по schema. Возвращает строку ошибки или None.
    """
    for key, meta in schema.items():
        if key not in params:
            continue
        value = params[key]
        ptype = meta.get("type", "str")

        # Проверка типа
        if ptype == "int":
            if not isinstance(value, (int, float)):
                return f"Параметр '{key}': ожидается число, получено {type(value).__name__}"
            value = int(value)
        elif ptype == "float":
            if not isinstance(value, (int, float)):
                return f"Параметр '{key}': ожидается число, получено {type(value).__name__}"
            value = float(value)

        # Проверка диапазона
        if ptype in ("int", "float"):
            if "min" in meta and value < meta["min"]:
                return f"Параметр '{key}': {value} < min({meta['min']})"
            if "max" in meta and value > meta["max"]:
                return f"Параметр '{key}': {value} > max({meta['max']})"

        # Проверка choice
        if ptype in ("choice", "select"):
            options = meta.get("options", [])
            if options and value not in options:
                return f"Параметр '{key}': '{value}' не в допустимых значениях {options}"

    return None


class StrategyLoader:
    def __init__(self):
        self._loaded: dict[str, LoadedStrategy] = {}
        self._lock = threading.Lock()

    def load(self, strategy_id: str, file_path: str) -> LoadedStrategy:
        path = Path(file_path)
        if not path.is_absolute():
            from config.settings import BASE_DIR
            path = BASE_DIR / path
        if not path.exists():
            raise StrategyLoadError(f"Файл не найден: {file_path}")
        if path.suffix != ".py":
            raise StrategyLoadError(f"Ожидается .py файл, получен: {path.suffix}")

        module = self._import_module(strategy_id, path)
        self._validate_module(strategy_id, module)
        loaded = LoadedStrategy(strategy_id, module, file_path)

        with self._lock:
            self._loaded[strategy_id] = loaded

        logger.info(
            f"Стратегия загружена: [{strategy_id}] "
            f"→ {loaded.info.get('name')} v{loaded.info.get('version', '?')}"
        )
        return loaded

    def get(self, strategy_id: str) -> Optional[LoadedStrategy]:
        """Возвращает уже загруженную стратегию из кэша, или None."""
        return self._loaded.get(strategy_id)

    def reload(self, strategy_id: str) -> LoadedStrategy:
        with self._lock:
            existing = self._loaded.get(strategy_id)
        if not existing:
            raise StrategyLoadError(
                f"Стратегия [{strategy_id}] не загружена. Используй load() сначала."
            )
        # Вызываем on_stop перед перезагрузкой, если стратегия была запущена
        if existing.state == StrategyState.RUNNING:
            logger.info(f"[{strategy_id}] Вызов on_stop перед перезагрузкой...")
            params = {k: v["default"] for k, v in existing.params_schema.items()}
            existing.call_on_stop(params, None)
        logger.info(f"Перезагрузка стратегии [{strategy_id}]...")
        return self.load(strategy_id, existing.file_path)

    def unload(self, strategy_id: str) -> bool:
        with self._lock:
            if strategy_id not in self._loaded:
                return False
            del self._loaded[strategy_id]
        logger.info(f"Стратегия [{strategy_id}] выгружена из памяти")
        return True

    def get_all(self) -> dict[str, LoadedStrategy]:
        with self._lock:
            return dict(self._loaded)

    def is_loaded(self, strategy_id: str) -> bool:
        return strategy_id in self._loaded

    def _import_module(self, strategy_id: str, path: Path):
        try:
            spec = importlib.util.spec_from_file_location(f"strategy_{strategy_id}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        except SyntaxError as e:
            raise StrategyLoadError(
                f"[{strategy_id}] Синтаксическая ошибка в {path.name}: {e}"
            )
        except Exception as e:
            raise StrategyLoadError(
                f"[{strategy_id}] Не удалось импортировать {path.name}: {e}\n"
                f"{traceback.format_exc()}"
            )

    def _validate_module(self, strategy_id: str, module):
        missing = [
            fn for fn in REQUIRED_FUNCTIONS
            if not hasattr(module, fn) or not callable(getattr(module, fn))
        ]
        if missing:
            raise StrategyLoadError(
                f"[{strategy_id}] Отсутствуют обязательные функции: {', '.join(missing)}"
            )
        try:
            info = module.get_info()
            if not isinstance(info, dict):
                raise StrategyLoadError(f"[{strategy_id}] get_info() должна возвращать dict")
            if "name" not in info:
                raise StrategyLoadError(f"[{strategy_id}] get_info() должна содержать ключ 'name'")
        except StrategyLoadError:
            raise
        except Exception as e:
            raise StrategyLoadError(f"[{strategy_id}] Ошибка при вызове get_info(): {e}")

        try:
            params = module.get_params()
            if not isinstance(params, dict):
                raise StrategyLoadError(f"[{strategy_id}] get_params() должна возвращать dict")
        except StrategyLoadError:
            raise
        except Exception as e:
            raise StrategyLoadError(f"[{strategy_id}] Ошибка при вызове get_params(): {e}")

        logger.debug(f"[{strategy_id}] Валидация пройдена успешно")


strategy_loader = StrategyLoader()
