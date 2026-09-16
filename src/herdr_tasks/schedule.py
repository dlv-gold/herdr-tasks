"""Calendar boundaries use local wall time, persisted times use UTC."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import Config


def utcnow() -> datetime:
    return datetime.now(UTC)


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def boundary(day: date, clock: str, zone: str) -> datetime:
    """First occurrence on a fold; first valid minute after a DST gap."""
    tz = ZoneInfo(zone)
    naive = datetime.combine(day, time.fromisoformat(clock))
    for _ in range(181):
        candidate = naive.replace(tzinfo=tz, fold=0)
        result = candidate.astimezone(UTC)
        if result.astimezone(tz).replace(tzinfo=None) == naive:
            return result
        naive += timedelta(minutes=1)
    raise ValueError("No valid schedule time near this boundary")


def latest(now: datetime, config: Config, weekly: bool = False) -> datetime:
    day = now.astimezone(ZoneInfo(config.timezone)).date()
    clock = config.weekly_time if weekly else config.daily_time
    if weekly:
        day -= timedelta(days=(day.weekday() - config.weekly_day) % 7)
    result = boundary(day, clock, config.timezone)
    if result > now.astimezone(UTC):
        result = boundary(day - timedelta(days=7 if weekly else 1), clock, config.timezone)
    return result


def next_boundary(now: datetime, config: Config, weekly: bool = False) -> datetime:
    prior = latest(now, config, weekly).astimezone(ZoneInfo(config.timezone)).date()
    return boundary(
        prior + timedelta(days=7 if weekly else 1),
        config.weekly_time if weekly else config.daily_time,
        config.timezone,
    )


def period_keys(now: datetime, config: Config) -> tuple[str, str]:
    """The active day and week are named by their starting local dates."""
    zone = ZoneInfo(config.timezone)
    return latest(now, config).astimezone(zone).date().isoformat(), latest(
        now, config, True
    ).astimezone(zone).date().isoformat()
