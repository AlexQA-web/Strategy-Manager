"""Тесты расписания: is_in_time_window и parse_schedule_window (TASK-016)."""

from datetime import time as dtime

import pytest

from core.scheduler import is_in_time_window, parse_schedule_window


class TestParseScheduleWindow:

    def test_valid_schedule(self):
        sched = {"connect_time": "09:30", "disconnect_time": "18:45", "days": [0, 1, 2]}
        result = parse_schedule_window(sched)
        assert result == (dtime(9, 30), dtime(18, 45), [0, 1, 2])

    def test_defaults(self):
        result = parse_schedule_window({})
        assert result == (dtime(6, 50), dtime(23, 45), [0, 1, 2, 3, 4])

    def test_invalid_time_returns_none(self):
        assert parse_schedule_window({"connect_time": "bad"}) is None

    def test_non_dict_returns_none(self):
        assert parse_schedule_window("not a dict") is None


class TestIsInTimeWindow:

    # ── Обычное окно (connect < disconnect) ───────────────────────────

    def test_intraday_inside(self):
        assert is_in_time_window(
            dtime(9, 0), dtime(18, 0), [0, 1, 2, 3, 4],
            now_weekday=2, now_time=dtime(12, 0),
        ) is True

    def test_intraday_before_open(self):
        assert is_in_time_window(
            dtime(9, 0), dtime(18, 0), [0, 1, 2, 3, 4],
            now_weekday=2, now_time=dtime(8, 59),
        ) is False

    def test_intraday_after_close(self):
        assert is_in_time_window(
            dtime(9, 0), dtime(18, 0), [0, 1, 2, 3, 4],
            now_weekday=2, now_time=dtime(18, 1),
        ) is False

    def test_intraday_at_open_boundary(self):
        assert is_in_time_window(
            dtime(9, 0), dtime(18, 0), [0, 1, 2, 3, 4],
            now_weekday=0, now_time=dtime(9, 0),
        ) is True

    def test_intraday_at_close_boundary(self):
        assert is_in_time_window(
            dtime(9, 0), dtime(18, 0), [0, 1, 2, 3, 4],
            now_weekday=0, now_time=dtime(18, 0),
        ) is True

    def test_intraday_wrong_day(self):
        assert is_in_time_window(
            dtime(9, 0), dtime(18, 0), [0, 1, 2, 3, 4],
            now_weekday=5, now_time=dtime(12, 0),  # суббота
        ) is False

    # ── Overnight окно (connect > disconnect) ─────────────────────────

    def test_overnight_evening_part(self):
        """22:00-02:00, сейчас 23:00 в пн — должно быть True."""
        assert is_in_time_window(
            dtime(22, 0), dtime(2, 0), [0, 1, 2, 3, 4],
            now_weekday=0, now_time=dtime(23, 0),
        ) is True

    def test_overnight_morning_part(self):
        """22:00-02:00, сейчас 01:00 во вт — должно быть True (пн в days)."""
        assert is_in_time_window(
            dtime(22, 0), dtime(2, 0), [0, 1, 2, 3, 4],
            now_weekday=1, now_time=dtime(1, 0),
        ) is True

    def test_overnight_morning_after_friday(self):
        """22:00-02:00, days=Пн-Пт, сейчас 01:00 в сб — True (пт в days)."""
        assert is_in_time_window(
            dtime(22, 0), dtime(2, 0), [0, 1, 2, 3, 4],
            now_weekday=5, now_time=dtime(1, 0),
        ) is True

    def test_overnight_morning_sunday_not_in_window(self):
        """22:00-02:00, days=Пн-Пт, сейчас 01:00 вс — False (сб не в days)."""
        assert is_in_time_window(
            dtime(22, 0), dtime(2, 0), [0, 1, 2, 3, 4],
            now_weekday=6, now_time=dtime(1, 0),
        ) is False

    def test_overnight_gap_midday(self):
        """22:00-02:00, сейчас 15:00 — ни вечер, ни утро → False."""
        assert is_in_time_window(
            dtime(22, 0), dtime(2, 0), [0, 1, 2, 3, 4],
            now_weekday=2, now_time=dtime(15, 0),
        ) is False

    def test_overnight_at_connect_boundary(self):
        assert is_in_time_window(
            dtime(22, 0), dtime(2, 0), [0, 1, 2, 3, 4],
            now_weekday=0, now_time=dtime(22, 0),
        ) is True

    def test_overnight_at_disconnect_boundary(self):
        assert is_in_time_window(
            dtime(22, 0), dtime(2, 0), [0, 1, 2, 3, 4],
            now_weekday=1, now_time=dtime(2, 0),
        ) is True

    def test_overnight_sunday_evening_not_in_days(self):
        """22:00-02:00, days=Пн-Пт, сейчас 23:00 вс — False."""
        assert is_in_time_window(
            dtime(22, 0), dtime(2, 0), [0, 1, 2, 3, 4],
            now_weekday=6, now_time=dtime(23, 0),
        ) is False
