# core/autostart.py

import threading
import time
from typing import Any, Dict, Optional
from loguru import logger


def autoconnect_connectors() -> None:
    """Автоподключение коннекторов по расписанию."""
    from core.storage import get_bool_setting, get_all_schedules
    from core.connector_manager import connector_manager, register_connectors
    from core.scheduler import strategy_scheduler, is_in_schedule

    # Регистрируем коннекторы (отложенная инициализация)
    register_connectors()
    # Планировщик мог стартовать до регистрации коннекторов,
    # поэтому после регистрации обязательно пересобираем cron-задачи.
    strategy_scheduler.setup_connector_schedule()

    if not get_bool_setting("autoconnect"):
        return

    schedules = get_all_schedules()

    for cid, sched in schedules.items():
        if not isinstance(sched, dict) or not sched.get("is_active", True):
            continue

        if is_in_schedule(cid):
            connector = connector_manager.get(cid)
            if connector:
                logger.info(f"Автозапуск: подключаем [{cid}]...")
                threading.Thread(target=connector.connect, daemon=True).start()
_live_engines: Dict[str, Any] = {}  # strategy_id → LiveEngine

# Единый lock для атомарных проверок состояния движков
_engine_state_lock = threading.Lock()

# Защита от двойного запуска: стратегии "в процессе запуска"
_launching_engines: Dict[str, bool] = {}


def get_live_engines() -> Dict[str, Any]:
    """Возвращает копию словаря запущенных LiveEngine (потокобезопасно)."""
    with _engine_state_lock:
        return dict(_live_engines)


def stop_live_engine(strategy_id: str) -> bool:
    """Останавливает LiveEngine стратегии и удаляет из реестра.

    Returns:
        True если engine был найден и остановлен, False если не найден.
    """
    from core.storage import get_strategy
    from core.strategy_loader import strategy_loader
    from core.connector_manager import connector_manager

    with _engine_state_lock:
        engine = _live_engines.get(strategy_id)
        if engine is None:
            logger.warning(f"[autostart] LiveEngine для стратегии '{strategy_id}' не найден")
            return False
        del _live_engines[strategy_id]

    try:
        engine.stop()
    finally:
        data = get_strategy(strategy_id) or {}
        loaded = strategy_loader.get(strategy_id)
        connector_id = data.get('connector_id') or data.get('connector') or 'finam'
        connector = connector_manager.get(connector_id)
        if loaded is not None:
            params = {k: v['default'] for k, v in loaded.params_schema.items()}
            params.update(data.get('params', {}))
            loaded.call_on_stop(params, connector)

    logger.info(f"[autostart] LiveEngine стратегии '{strategy_id}' остановлен и удалён")
    return True


def start_live_engine(strategy_id: str, wait_for_connection: bool = True) -> bool:
    """Запускает стратегию и её LiveEngine по конфигу из хранилища."""
    from core.storage import get_strategy
    from core.strategy_loader import strategy_loader
    from core.connector_manager import connector_manager
    from core.live_engine import LiveEngine
    from core.scheduler import is_in_schedule

    data = get_strategy(strategy_id)
    if not data:
        logger.warning(f"[autostart] Стратегия '{strategy_id}' не найдена")
        return False

    file_path = data.get('file_path') or data.get('file', '')
    if not file_path:
        logger.warning(f"[autostart] [{strategy_id}] не указан путь к файлу")
        return False

    connector_id = data.get('connector_id') or data.get('connector') or 'finam'
    connector = connector_manager.get(connector_id)
    if connector is None:
        logger.warning(f"[autostart] [{strategy_id}] коннектор '{connector_id}' не найден")
        return False

    loaded = strategy_loader.get(strategy_id)
    if loaded is None:
        loaded = strategy_loader.load(strategy_id, file_path)

    params = {k: v['default'] for k, v in loaded.params_schema.items()}
    params.update(data.get('params', {}))

    # Миграция order_mode: если на верхнем уровне — перенести в params
    order_mode = data.get('order_mode')
    if order_mode is not None and 'order_mode' not in params:
        params['order_mode'] = order_mode
        logger.info(f"[autostart] [{strategy_id}] order_mode мигрирован в params: {params['order_mode']}")

    # Атомарная проверка: уже запущен или в процессе запуска
    with _engine_state_lock:
        if strategy_id in _live_engines:
            logger.info(f"[autostart] [{strategy_id}] LiveEngine уже запущен")
            return True
        if _launching_engines.get(strategy_id, False):
            logger.info(f"[autostart] [{strategy_id}] LiveEngine уже в процессе запуска — пропуск")
            return False
        _launching_engines[strategy_id] = True

    try:
        def _wait_for_connector(conn, timeout=120):
            """Ждёт подключения коннектора с таймаутом."""
            deadline = time.time() + timeout
            while time.time() < deadline:
                if conn.is_connected():
                    return True
                time.sleep(1)
            return False

        if not loaded.call_on_start(params, connector):
            return False

        logger.info(f"Автозапуск: [{strategy_id}] запущена")

        if hasattr(loaded.module, 'on_bar') and connector:
            if not connector.is_connected():
                if wait_for_connection:
                    if not is_in_schedule(connector_id):
                        logger.info(
                            f"Автозапуск [{strategy_id}]: коннектор вне расписания, LiveEngine не запущен"
                        )
                        return False
                    logger.info(f"Автозапуск [{strategy_id}]: ожидаем подключения коннектора...")
                    if not _wait_for_connector(connector, timeout=30):
                        logger.warning(
                            f"Автозапуск [{strategy_id}]: коннектор не подключился за 30с, LiveEngine не запущен"
                        )
                        return False
                else:
                    logger.warning(
                        f"[autostart] [{strategy_id}] коннектор '{connector_id}' не подключён, LiveEngine не запущен"
                    )
                    return False
            # Дополнительная валидация перед стартом: отказ если офлайн
            if not connector.is_connected():
                logger.warning(
                    f"[autostart] [{strategy_id}] коннектор '{connector_id}' офлайн, запуск отменён"
                )
                return False
            try:
                engine = LiveEngine(
                    strategy_id=strategy_id,
                    loaded_strategy=loaded,
                    params=params,
                    connector=connector,
                    account_id=data.get('account_id') or data.get('finam_account', ''),
                    ticker=data.get('ticker', params.get('ticker', '')),
                    board=data.get('board', 'FUT'),
                    timeframe=data.get('timeframe', '5'),
                    agent_name=strategy_id,
                    order_mode=params.get('order_mode', 'market'),
                    lot_sizing=data.get('lot_sizing', {}),
                )
                engine.start()
                with _engine_state_lock:
                    _live_engines[strategy_id] = engine
                logger.info(f"Автозапуск: [{strategy_id}] LiveEngine запущен")
            except Exception as e:
                logger.error(f"Автозапуск [{strategy_id}]: ошибка LiveEngine — {e}")
                return False

        return True
    finally:
        # Снимаем флаг запуска независимо от результата
        with _engine_state_lock:
            _launching_engines.pop(strategy_id, None)


def autostart_strategies() -> None:
    """Автозапуск активных стратегий (вызывается в фоновом потоке)."""
    from core.storage import get_bool_setting, get_all_strategies

    if not get_bool_setting("autostart_strategies"):
        return

    def _run():
        time.sleep(3)  # Даём время на инициализацию

        strategies = get_all_strategies()
        started = 0

        for sid, data in strategies.items():
            if not (data.get("status") == "active" and data.get("is_enabled", True)):
                continue

            file_path = data.get("file_path") or data.get("file", "")
            if not file_path:
                logger.warning(f"Автозапуск [{sid}]: не указан путь к файлу — пропуск")
                continue

            try:
                if start_live_engine(sid, wait_for_connection=True):
                    started += 1
            except Exception as e:
                logger.error(f"Автозапуск [{sid}]: ошибка — {e}")

        logger.info(f"Автозапуск: запущено стратегий: {started}")

    threading.Thread(target=_run, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# Watchdog: автоматическая синхронизация движков с состоянием коннекторов
# ─────────────────────────────────────────────────────────────────────────────

# Кэш последних известных состояний коннекторов {connector_id: bool}
# Нужен чтобы реагировать только на ИЗМЕНЕНИЕ состояния, а не опрашивать постоянно
_connector_states: dict[str, bool] = {}
_connector_states_lock = threading.Lock()

_watchdog_stop_event = threading.Event()


def _sync_engines_with_connectors() -> None:
    """
    Ядро watchdog: синхронизирует запущенные движки с состоянием коннекторов.

    Логика:
    - Если коннектор только что ПОДКЛЮЧИЛСЯ (был False → стал True):
        для всех активных стратегий этого коннектора, у которых нет движка → запускаем.
    - Если коннектор только что ОТКЛЮЧИЛСЯ (был True → стал False):
        для всех движков этого коннектора → останавливаем.
    - Если состояние не изменилось — ничего не делаем (тихий опрос).
    """
    from core.storage import get_all_strategies, get_bool_setting
    from core.connector_manager import connector_manager
    from core.scheduler import is_in_schedule

    if not get_bool_setting("autostart_strategies"):
        return

    # Снимаем текущий статус всех зарегистрированных коннекторов
    current_states: dict[str, bool] = {}
    for cid, conn in connector_manager.all().items():
        try:
            current_states[cid] = conn.is_connected()
        except Exception:
            current_states[cid] = False

    # Определяем что изменилось
    just_connected: set[str] = set()
    just_disconnected: set[str] = set()

    with _connector_states_lock:
        for cid, is_conn in current_states.items():
            prev = _connector_states.get(cid)
            if prev is None:
                # Первый опрос — запоминаем состояние, не запускаем/останавливаем
                _connector_states[cid] = is_conn
                continue
            if prev is False and is_conn is True:
                just_connected.add(cid)
            elif prev is True and is_conn is False:
                just_disconnected.add(cid)
            _connector_states[cid] = is_conn

    # ── Коннектор подключился → запускаем движки ─────────────────────────────
    if just_connected:
        strategies = get_all_strategies()
        engines = get_live_engines()

        for cid in just_connected:
            logger.info(f"[Watchdog] Коннектор [{cid}] подключился — проверяем стратегии")

            if not is_in_schedule(cid):
                logger.info(f"[Watchdog] [{cid}] вне окна расписания — движки не запускаем")
                continue

            for sid, data in strategies.items():
                if not (data.get("status") == "active" and data.get("is_enabled", True)):
                    continue
                sid_connector = data.get("connector_id") or data.get("connector") or "finam"
                if sid_connector != cid:
                    continue
                if sid in engines:
                    continue  # уже запущен

                file_path = data.get("file_path") or data.get("file", "")
                if not file_path:
                    continue

                try:
                    logger.info(f"[Watchdog] Запускаем [{sid}] — коннектор [{cid}] подключился")
                    start_live_engine(sid, wait_for_connection=False)
                except Exception as e:
                    logger.error(f"[Watchdog] Ошибка запуска [{sid}]: {e}")

    # ── Коннектор отключился → останавливаем движки ───────────────────────────
    if just_disconnected:
        engines = get_live_engines()
        strategies = get_all_strategies()

        for cid in just_disconnected:
            logger.info(f"[Watchdog] Коннектор [{cid}] отключился — останавливаем движки")

            for sid, engine in engines.items():
                data = strategies.get(sid, {})
                sid_connector = data.get("connector_id") or data.get("connector") or "finam"
                if sid_connector != cid:
                    continue
                try:
                    logger.info(f"[Watchdog] Останавливаем [{sid}] — коннектор [{cid}] отключился")
                    stop_live_engine(sid)
                except Exception as e:
                    logger.error(f"[Watchdog] Ошибка остановки [{sid}]: {e}")


def start_engine_watchdog(interval_sec: int = 15) -> None:
    """
    Запускает фоновый поток watchdog, который следит за состоянием коннекторов
    и автоматически запускает/останавливает движки стратегий.

    Вызывать один раз при старте приложения (после autostart_strategies).

    Args:
        interval_sec: Как часто проверять состояние коннекторов (по умолчанию 15 сек).
    """
    _watchdog_stop_event.clear()

    def _loop():
        # Первый опрос — только запоминаем начальное состояние, без действий
        time.sleep(5)
        _sync_engines_with_connectors()  # инициализирует _connector_states

        logger.info(f"[Watchdog] Запущен (интервал {interval_sec}с)")

        while not _watchdog_stop_event.is_set():
            _watchdog_stop_event.wait(timeout=interval_sec)
            if _watchdog_stop_event.is_set():
                break
            try:
                _sync_engines_with_connectors()
            except Exception as e:
                logger.error(f"[Watchdog] Ошибка в цикле: {e}")

        logger.info("[Watchdog] Остановлен")

    threading.Thread(target=_loop, daemon=True, name="EngineWatchdog").start()


def stop_engine_watchdog() -> None:
    """Останавливает watchdog (вызывать при завершении приложения)."""
    _watchdog_stop_event.set()
