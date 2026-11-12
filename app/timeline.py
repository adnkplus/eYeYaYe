"""Helpers for the synchronized 24-hour playback timeline."""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from typing import Iterable
import re


def parse_day_start(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value))
    if not match:
        raise ValueError(f"Invalid archive day start: {value!r}; expected HH:MM.")
    hour, minute = map(int, match.groups())
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"Invalid archive day start: {value!r}; expected HH:MM.")
    return hour, minute


def archive_day_for(moment: datetime, day_start: str = "05:00") -> date:
    """Return the archive-day label that contains `moment` in its local timezone."""
    hour, minute = parse_day_start(day_start)
    local = moment.astimezone() if moment.tzinfo is None else moment
    boundary = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return local.date() if local >= boundary else (local - timedelta(days=1)).date()


def day_window(day_value: str | date, day_start: str = "05:00", tz=None) -> tuple[datetime, datetime]:
    """Return local archive-day endpoints; DST days may be 23 or 25 elapsed hours."""
    day = date.fromisoformat(day_value) if isinstance(day_value, str) else day_value
    hour, minute = parse_day_start(day_start)
    tz = tz or datetime.now().astimezone().tzinfo
    start = datetime.combine(day, dtime(hour, minute), tzinfo=tz)
    next_day = day + timedelta(days=1)
    end = datetime.combine(next_day, dtime(hour, minute), tzinfo=tz)
    return start, end


def record_source_intervals(record: dict) -> list[tuple[float, float]]:
    """Return wall-clock source intervals covered by a record."""
    intervals = []
    for item in record.get("time_map") or []:
        try:
            start = datetime.fromisoformat(item["source_start"]).timestamp()
            end = datetime.fromisoformat(item["source_end"]).timestamp()
            if end > start:
                intervals.append((start, end))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    if intervals:
        return intervals
    try:
        start = float(record.get("start_ts", 0))
        duration = float(record.get("duration_seconds", 0) or 0)
    except (TypeError, ValueError):
        return []
    return [(start, start + duration)] if duration > 0 else []


def output_position_for_wall_time(record: dict, wall_time: float) -> float | None:
    """Translate a wall-clock timestamp into a file seek position."""
    time_map = record.get("time_map") or []
    for item in time_map:
        try:
            source_start = datetime.fromisoformat(item["source_start"]).timestamp()
            source_end = datetime.fromisoformat(item["source_end"]).timestamp()
            if source_start <= wall_time < source_end:
                output_start = float(item.get("output_start_seconds", 0))
                speed = max(0.001, float(item.get("speed", 1.0)))
                return max(0.0, output_start + (wall_time - source_start) / speed)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    intervals = record_source_intervals(record)
    if intervals and intervals[0][0] <= wall_time < intervals[-1][1]:
        return max(0.0, wall_time - float(record.get("start_ts", intervals[0][0])))
    return None


def choose_record(records: Iterable[dict], camera_id: str, wall_time: float) -> tuple[dict, float] | None:
    """Choose the best available recording for a camera and wall-clock instant.

    RAW is preferred while retained; otherwise MER, then intelligent/regular time-lapse.
    """
    priority = {"RAW": 0, "MER": 1, "TLI": 2, "TLR": 3}
    candidates = []
    for record in records:
        if str(record.get("camera_id")) != str(camera_id):
            continue
        position = output_position_for_wall_time(record, wall_time)
        if position is None:
            continue
        candidates.append((priority.get(str(record.get("type_code", "")).upper(), 9),
                           float(record.get("start_ts", 0)), record, position))
    if not candidates:
        return None
    _, _, record, position = min(candidates, key=lambda x: (x[0], -x[1]))
    return record, position


def next_record(records: Iterable[dict], camera_id: str, wall_time: float) -> tuple[dict, float, float] | None:
    """Find the next available source interval for a camera at/after wall_time."""
    candidates = []
    for record in records:
        if str(record.get("camera_id")) != str(camera_id):
            continue
        for start, end in record_source_intervals(record):
            target = max(start, wall_time)
            if end <= target:
                continue
            pos = output_position_for_wall_time(record, target)
            if pos is not None:
                priority = {"RAW": 0, "MER": 1, "TLI": 2, "TLR": 3}.get(
                    str(record.get("type_code", "")).upper(), 9)
                candidates.append((target, priority, record, pos, end))
    if not candidates:
        return None
    start, _, record, pos, end = min(candidates, key=lambda x: (x[0], x[1]))
    return record, pos, start


def timeline_fraction(wall_time: float, start: float, end: float) -> float:
    if end <= start:
        return 0.0
    return max(0.0, min(1.0, (float(wall_time) - start) / (end - start)))
