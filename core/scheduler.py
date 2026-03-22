# core/scheduler.py

from loguru import logger
from apscheduler.schedulers.background import BackgroundScheduler
from core.storage import get_all_schedules

DAYS_RU = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}


def is_in_schedule(connector_id: str) -> bool:
    """Возвращает True если коннектор сейчас находится в окне работы по расписанию.

    Используется в autostart для проверки перед запуском LiveEngine.
    Если расписание не найдено или неактивно — возвращает True (не блокируем).
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    from datetime import datetime, time as dtime

    schedules = get_all_schedules()
    sched = schedules.get(connector_id)
    if not sched or not isinstance(sched, dict):
        return True  # нет расписания — не блокируем
    if not sched.get("is_active", True):
        return True

    now_msk = datetime.now(ZoneInfo("Europe/Moscow"))
    today = now_msk.weekday()
    if today not in sched.get("days", [0, 1, 2, 3, 4]):
        return False

    now_t = now_msk.time().replace(second=0, microsecond=0)
    try:
        ch, cm = map(int, sched.get("connect_time",    "06:50").split(":"))
        dh, dm = map(int, sched.get("disconnect_time", "23:45").split(":"))
    except Exception as e:
        logger.warning(f"[Scheduler] is_in_schedule({connector_id}): ошибка парсинга времени: {e}")
        return True

    connect_t    = dtime(ch, cm)
    disconnect_t = dtime(dh, dm)
    if connect_t <= disconnect_t:
        return connect_t <= now_t <= disconnect_t
    else:
        return now_t >= connect_t or now_t <= disconnect_t


class StrategyScheduler:

    def __init__(self):
        self._scheduler = BackgroundScheduler(timezone="Europe/Moscow")

    def start(self):
        self._scheduler.start()
        logger.info("Планировщик запущен (timezone: Europe/Moscow)")
        self.setup_connector_schedule()

    def stop(self):
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        logger.info("Планировщик остановлен")

    def setup_connector_schedule(self):
        from core.connector_manager import connector_manager

        self._scheduler.remove_all_jobs()
        schedules = get_all_schedules()

        for cid, sched in schedules.items():
            if not isinstance(sched, dict):
                logger.warning(f"[Scheduler] Пропуск {cid}: ожидался dict")
                continue
            if not sched.get("is_active", True):
                continue

            connector = connector_manager.get(cid)
            if not connector:
                continue

            days         = sched.get("days", [0, 1, 2, 3, 4])
            connect_t    = sched.get("connect_time", "06:50")
            disconnect_t = sched.get("disconnect_time", "23:45")

            if not days:
                logger.debug(f"[Scheduler] {cid}: нет дней — пропуск")
                continue

            try:
                ch, cm = map(int, connect_t.split(":"))
                dh, dm = map(int, disconnect_t.split(":"))
            except ValueError:
                logger.error(f"[Scheduler] {cid}: неверный формат времени")
                continue

            day_str = ",".join(str(d) for d in days)

            self._scheduler.add_job(
                connector.connect,
                trigger="cron",
                day_of_week=day_str,
                hour=ch, minute=cm,
                id=f"{cid}_connect",
                replace_existing=True,
                misfire_grace_time=60,
            )
            self._scheduler.add_job(
                connector.disconnect,
                trigger="cron",
                day_of_week=day_str,
                hour=dh, minute=dm,
                id=f"{cid}_disconnect",
                replace_existing=True,
                misfire_grace_time=60,
            )
            logger.info(
                f"[Scheduler] {cid}: "
                f"connect {connect_t}, disconnect {disconnect_t}, "
                f"days={[DAYS_RU[d] for d in days]}"
            )

    def get_next_events(self, limit: int = 5) -> list[dict]:
        events = []
        for job in self._scheduler.get_jobs():
            if job.next_run_time:
                events.append({
                    "job_id":      job.id,
                    "name":        job.name,
                    "next_run":    job.next_run_time.strftime("%d.%m %H:%M"),
                    "next_run_dt": job.next_run_time,
                })
        events.sort(key=lambda x: x["next_run_dt"])
        return events[:limit]


strategy_scheduler = StrategyScheduler()
