"""Timestamps and working hours. Every stored timestamp is a UTC ISO-8601 string."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Schedule

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


class Clock:
    def __init__(self, schedule: Schedule, now: Callable[[], datetime] = utcnow):
        self.tz = ZoneInfo(schedule.timezone)
        self.days = {day.strip().lower()[:3] for day in schedule.days}
        self.start = time.fromisoformat(schedule.start)
        self.end = time.fromisoformat(schedule.end)
        self.hold_days = {day.strip().lower()[:3] for day in schedule.hold_days}
        self.hold_channels = set(schedule.hold_channels)
        self.send_start = time.fromisoformat(schedule.send_start)
        self.send_end = time.fromisoformat(schedule.send_end)
        self._now = now

    def now(self) -> datetime:
        return self._now()

    def stamp(self) -> str:
        return iso(self._now())

    def local(self) -> datetime:
        return self._now().astimezone(self.tz)

    def working(self) -> bool:
        here = self.local()
        return WEEKDAYS[here.weekday()] in self.days and self.start <= here.time() < self.end

    def holding(self, channel: str) -> bool:
        """Whether this channel waits right now: by default emails and LinkedIn messages go out only
        Monday to Saturday, between send_start and send_end."""
        return channel in self.hold_channels and (self.held_day() or not self.send_hours())

    def held_day(self) -> bool:
        return WEEKDAYS[self.local().weekday()] in self.hold_days

    def send_hours(self) -> bool:
        return self.send_start <= self.local().time() < self.send_end

    def day_start(self) -> str:
        """Midnight today in your timezone, as a UTC stamp. Daily caps count from here."""
        return iso(self.local().replace(hour=0, minute=0, second=0, microsecond=0))

    def ago(self, **delta: float) -> str:
        return iso(self._now() - timedelta(**delta))

    def later(self, **delta: float) -> str:
        return iso(self._now() + timedelta(**delta))
