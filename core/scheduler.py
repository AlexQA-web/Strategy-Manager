# core/scheduler.py

from datetime import time as dtime
from typing import Optional

from loguru import logger
from apscheduler.schedulers.background import BackgroundScheduler
from core.storage import get_all_schedules

DAYS_RU = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}
MOEX_COMMISSION_REFRESH_HOUR = 5
MOEX_COMMISSION_REFRESH_MINUTE = 0


def parse_schedule_window(sched: dict) -> Optional[tuple[dtime, dtime, list[int]]]:
    """Парсит расписание и возвращает (connect_time, disconnect_time, days).

    Возвращает None при невалидном расписании.
    """
    if not isinstance(sched, dict):
        return None
    try:
        ch, cm = map(int, sched.get("connect_time", "06:50").split(":"))
        dh, dm = map(int, sched.get("disconnect_time", "23:45").split(":"))
    except Exception:
        return None
    connect_t = dtime(ch, cm)
    disconnect_t = dtime(dh, dm)
    days = sched.get("days", [0, 1, 2, 3, 4])
    return connect_t, disconnect_t, days


def is_in_time_window(
    connect_t: dtime,
    disconnect_t: dtime,
    days: list[int],
    now_weekday: int,
    now_time: dtime,
) -> bool:
    """Проверяет, попадает ли (now_weekday, now_time) в окно расписания.

    Overnight логика (connect_t > disconnect_t):
    - now_time >= connect_t И today входит в days → True
    - now_time <= disconnect_t И yesterday входит в days → True
    """
    if connect_t <= disconnect_t:
        # Обычное окно в пределах одного дня
        return now_weekday in days and connect_t <= now_time <= disconnect_t
    else:
        # Overnight: переход через полночь
        if now_time >= connect_t and now_weekday in days:
            return True
        yesterday = (now_weekday - 1) % 7
        if now_time <= disconnect_t and yesterday in days:
            return True
        return False


def is_in_schedule(connector_id: str) -> bool:
    """Возвращает True если коннектор сейчас находится в окне работы по расписанию.

    Используется в autostart и reconnect-loop для проверки допустимости работы.
    Если расписание не найдено или неактивно — возвращает True (не блокируем).
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    from datetime import datetime

    schedules = get_all_schedules()
    sched = schedules.get(connector_id)
    if not sched or not isinstance(sched, dict):
        return True  # нет расписания — не блокируем
    if not sched.get("is_active", True):
        return True

    parsed = parse_schedule_window(sched)
    if parsed is None:
        logger.warning(f"[Scheduler] is_in_schedule({connector_id}): ошибка парсинга времени")
        return True

    connect_t, disconnect_t, days = parsed
    now_msk = datetime.now(ZoneInfo("Europe/Moscow"))
    now_time = now_msk.time().replace(second=0, microsecond=0)
    return is_in_time_window(connect_t, disconnect_t, days, now_msk.weekday(), now_time)


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
        from core.commission_manager import commission_manager

        self._scheduler.remove_all_jobs()
        self._scheduler.add_job(
            commission_manager.refresh_moex_rates,
            trigger="cron",
            hour=MOEX_COMMISSION_REFRESH_HOUR,
            minute=MOEX_COMMISSION_REFRESH_MINUTE,
            id="moex_commission_refresh",
            replace_existing=True,
            misfire_grace_time=300,
        )
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

            parsed = parse_schedule_window(sched)
            if parsed is None:
                logger.error(f"[Scheduler] {cid}: неверный формат времени")
                continue

            connect_t, disconnect_t, days = parsed
            if not days:
                logger.debug(f"[Scheduler] {cid}: нет дней — пропуск")
                continue

            connect_day_str = ",".join(str(d) for d in days)

            # Для overnight-расписания disconnect fires на следующий день
            if connect_t > disconnect_t:
                disconnect_days = [(d + 1) % 7 for d in days]
            else:
                disconnect_days = list(days)
            disconnect_day_str = ",".join(str(d) for d in disconnect_days)

            self._scheduler.add_job(
                connector.connect,
                trigger="cron",
                day_of_week=connect_day_str,
                hour=connect_t.hour, minute=connect_t.minute,
                id=f"{cid}_connect",
                replace_existing=True,
                misfire_grace_time=60,
            )
            self._scheduler.add_job(
                connector.disconnect,
                trigger="cron",
                day_of_week=disconnect_day_str,
                hour=disconnect_t.hour, minute=disconnect_t.minute,
                id=f"{cid}_disconnect",
                replace_existing=True,
                misfire_grace_time=60,
            )
            logger.info(
                f"[Scheduler] {cid}: "
                f"connect {connect_t.strftime('%H:%M')} days={[DAYS_RU[d] for d in days]}, "
                f"disconnect {disconnect_t.strftime('%H:%M')} days={[DAYS_RU[d] for d in disconnect_days]}"
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
