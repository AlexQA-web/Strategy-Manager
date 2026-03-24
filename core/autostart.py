# core/autostart.py

import threading
from loguru import logger


def autoconnect_connectors():
    """Автоподключение коннекторов по расписанию."""
    from core.storage import get_bool_setting, get_all_schedules
    from core.connector_manager import connector_manager, register_connectors
    from datetime import datetime, time as dtime
    
    # Регистрируем коннекторы (отложенная инициализация)
    register_connectors()

    if not get_bool_setting("autoconnect"):
        return

    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    schedules = get_all_schedules()
    now_msk = datetime.now(ZoneInfo("Europe/Moscow"))
    now_t = now_msk.time().replace(second=0, microsecond=0)
    today = now_msk.weekday()

    for cid, sched in schedules.items():
        if not isinstance(sched, dict) or not sched.get("is_active", True):
            continue
        if today not in sched.get("days", [0, 1, 2, 3, 4, 5, 6]):
            logger.info(f"Автозапуск [{cid}]: не торговый день — пропуск")
            continue

        try:
            ch, cm = map(int, sched.get("connect_time", "06:50").split(":"))
            dh, dm = map(int, sched.get("disconnect_time", "23:45").split(":"))
            t_open, t_close = dtime(ch, cm), dtime(dh, dm)
            if t_open <= t_close:
                in_window = t_open <= now_t <= t_close
            else:
                in_window = now_t >= t_open or now_t <= t_close
        except Exception as e:
            logger.warning(f"Автозапуск [{cid}]: ошибка парсинга расписания, пропуск: {e}")
            in_window = False

        if in_window:
            connector = connector_manager.get(cid)
            if connector:
                logger.info(f"Автозапуск: подключаем [{cid}]...")
                threading.Thread(target=connector.connect, daemon=True).start()
        else:
            logger.info(f"Автозапуск [{cid}]: вне окна работы — пропуск")


_live_engines: dict = {}  # strategy_id → LiveEngine
_live_engines_lock = threading.Lock()  # защита от race condition


def get_live_engines() -> dict:
    """Возвращает копию словаря запущенных LiveEngine (потокобезопасно)."""
    with _live_engines_lock:
        return dict(_live_engines)


def stop_live_engine(strategy_id: str) -> bool:
    """Останавливает LiveEngine стратегии и удаляет из реестра.
    
    Returns:
        True если engine был найден и остановлен, False если не найден.
    """
    from core.storage import get_strategy
    from core.strategy_loader import strategy_loader
    from core.connector_manager import connector_manager

    with _live_engines_lock:
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

    with _live_engines_lock:
        existing = _live_engines.get(strategy_id)
    if existing is not None:
        logger.info(f"[autostart] [{strategy_id}] LiveEngine уже запущен")
        return True

    def _wait_for_connector(conn, timeout=120):
        """Ждёт подключения коннектора с таймаутом."""
        import time
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
        try:
            engine = LiveEngine(
                strategy_id=strategy_id,
                loaded_strategy=loaded,
                params=params,
                connector=connector,
                account_id=data.get('finam_account', ''),
                ticker=data.get('ticker', params.get('ticker', '')),
                board=data.get('board', 'FUT'),
                timeframe=data.get('timeframe', '5'),
                agent_name=strategy_id,
                order_mode=data.get('order_mode', 'market'),
                lot_sizing=data.get('lot_sizing', {}),
            )
            engine.start()
            with _live_engines_lock:
                _live_engines[strategy_id] = engine
            logger.info(f"Автозапуск: [{strategy_id}] LiveEngine запущен")
        except Exception as e:
            logger.error(f"Автозапуск [{strategy_id}]: ошибка LiveEngine — {e}")
            return False

    return True


def autostart_strategies():
    """Автозапуск активных стратегий (вызывается в фоновом потоке)."""
    from core.storage import get_bool_setting, get_all_strategies
    from core.strategy_loader import strategy_loader
    from core.connector_manager import connector_manager
    from core.live_engine import LiveEngine
    from core.scheduler import is_in_schedule

    if not get_bool_setting("autostart_strategies"):
        return

    def _run():
        import time
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
