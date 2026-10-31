"""Schedule expansion, current/next programme selection and EPG source data."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from database import (
    Program,
    ScheduleEntry,
    db,
    enabled_programs,
    get_int_setting,
)
from utils import now_local


@dataclass
class Occurrence:
    entry_id: int | None
    program_id: int
    title: str
    description: str
    start: datetime
    end: datetime
    repeat_days: str = ""

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())

    @property
    def is_current(self) -> bool:
        return self.start <= now_local() < self.end

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "program_id": self.program_id,
            "title": self.title,
            "description": self.description,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "repeat_days": self.repeat_days,
        }


def program_duration_seconds(program: Program | None) -> float:
    if not program:
        return 0.0
    total = 0.0
    for item in program.items:
        if item.media and item.media.duration:
            total += float(item.media.duration)
    if total > 0:
        return total
    return float(max(1, get_int_setting("default_program_minutes", 30)) * 60)


def _parse_days(value: str | None) -> list[int]:
    days: list[int] = []
    for part in (value or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            day = int(part)
        except ValueError:
            continue
        if 0 <= day <= 6 and day not in days:
            days.append(day)
    return days


def _entry_occurrences(entry: ScheduleEntry, range_start: datetime, range_end: datetime) -> Iterable[Occurrence]:
    if not entry.program or not entry.enabled or not entry.program.enabled:
        return
    duration = timedelta(minutes=max(1, int(entry.duration_minutes or 30)))
    days = _parse_days(entry.repeat_days)
    if not days:
        start = entry.start_at
        end = start + duration
        if end > range_start and start < range_end:
            yield Occurrence(
                entry.id,
                entry.program_id,
                entry.program.title,
                entry.program.description or "",
                start,
                end,
                entry.repeat_days,
            )
        return
    start_date = (range_start - duration).date()
    end_date = range_end.date()
    current = start_date
    while current <= end_date:
        if current.weekday() in days:
            start = datetime.combine(current, entry.start_at.time())
            end = start + duration
            if end > range_start and start < range_end:
                yield Occurrence(
                    entry.id,
                    entry.program_id,
                    entry.program.title,
                    entry.program.description or "",
                    start,
                    end,
                    entry.repeat_days,
                )
        current += timedelta(days=1)


def occurrences(range_start: datetime, range_end: datetime) -> list[Occurrence]:
    entries = list(
        db.session.execute(
            select(ScheduleEntry)
            .options(joinedload(ScheduleEntry.program))
            .where(ScheduleEntry.enabled.is_(True))
            .order_by(ScheduleEntry.start_at.asc())
        )
        .scalars()
        .all()
    )
    result: list[Occurrence] = []
    for entry in entries:
        result.extend(_entry_occurrences(entry, range_start, range_end))
    result.sort(key=lambda item: (item.start, item.end))
    return result


def active_occurrence(now: datetime | None = None) -> Occurrence | None:
    now = now or now_local()
    items = occurrences(now - timedelta(days=1), now + timedelta(days=1))
    active = [item for item in items if item.start <= now < item.end]
    if not active:
        return None
    active.sort(key=lambda item: item.start)
    return active[-1]


def next_occurrence(now: datetime | None = None, days: int = 7) -> Occurrence | None:
    now = now or now_local()
    items = occurrences(now, now + timedelta(days=max(1, days)))
    future = [item for item in items if item.start > now]
    return future[0] if future else None


def upcoming(days: int = 7, now: datetime | None = None) -> list[Occurrence]:
    now = now or now_local()
    return occurrences(now - timedelta(hours=1), now + timedelta(days=max(1, days)))


def rotation_programs() -> list[Program]:
    return enabled_programs()


def rotation_first() -> Program | None:
    programs = rotation_programs()
    return programs[0] if programs else None


def rotation_next(current_program_id: int | None) -> Program | None:
    programs = rotation_programs()
    if not programs:
        return None
    if current_program_id is None:
        return programs[0]
    for index, program in enumerate(programs):
        if program.id == current_program_id:
            return programs[(index + 1) % len(programs)]
    return programs[0]


def choose_start_program(now: datetime | None = None) -> Program | None:
    now = now or now_local()
    active = active_occurrence(now)
    if active:
        program = db.session.get(Program, active.program_id)
        if program and program.enabled:
            return program
    return rotation_first()


def choose_next_program(
    current_program_id: int | None,
    now: datetime | None = None,
    respect_schedule: bool = True,
) -> Program | None:
    now = now or now_local()
    if respect_schedule:
        active = active_occurrence(now)
        if active and active.program_id != current_program_id:
            program = db.session.get(Program, active.program_id)
            if program and program.enabled:
                return program
        upcoming_entry = next_occurrence(now)
        if upcoming_entry and upcoming_entry.start <= now + timedelta(minutes=5):
            program = db.session.get(Program, upcoming_entry.program_id)
            if program and program.enabled and program.id != current_program_id:
                return program
    return rotation_next(current_program_id)


def schedule_epg_occurrences(days: int = 7, now: datetime | None = None) -> list[Occurrence]:
    """Scheduled occurrences, or a synthetic rotation when no schedule exists."""
    now = now or now_local()
    range_start = now - timedelta(hours=1)
    range_end = now + timedelta(days=max(1, days))
    scheduled = occurrences(range_start, range_end)
    if scheduled:
        return scheduled
    programs = rotation_programs()
    if not programs:
        return []
    result: list[Occurrence] = []
    cursor = range_start.replace(minute=0, second=0, microsecond=0)
    index = 0
    guard = 0
    while cursor < range_end and guard < 500:
        guard += 1
        program = programs[index % len(programs)]
        duration = timedelta(seconds=max(60, program_duration_seconds(program)))
        end = cursor + duration
        result.append(
            Occurrence(
                None,
                program.id,
                program.title,
                program.description or "",
                cursor,
                end,
                "",
            )
        )
        cursor = end
        index += 1
    return result


def timeline_for_day(day: date, now: datetime | None = None) -> list[dict]:
    now = now or now_local()
    start = datetime.combine(day, time.min)
    end = start + timedelta(days=1)
    items = occurrences(start, end)
    if not items:
        # Show the fallback rotation for the day so the UI is not empty.
        programs = rotation_programs()
        if programs:
            cursor = start
            index = 0
            guard = 0
            while cursor < end and guard < 200:
                guard += 1
                program = programs[index % len(programs)]
                duration = timedelta(seconds=max(60, program_duration_seconds(program)))
                item_end = min(end, cursor + duration)
                items.append(
                    Occurrence(None, program.id, program.title, program.description or "", cursor, item_end, "")
                )
                cursor = item_end
                index += 1
    return [
        {
            "title": item.title,
            "start": item.start,
            "end": item.end,
            "start_label": item.start.strftime("%H:%M"),
            "end_label": item.end.strftime("%H:%M"),
            "left": max(0.0, (item.start - start).total_seconds() / 86400 * 100),
            "width": max(0.3, (item.end - item.start).total_seconds() / 86400 * 100),
            "is_current": item.start <= now < item.end,
            "program_id": item.program_id,
        }
        for item in items
    ]


def schedule_status(now: datetime | None = None) -> dict:
    now = now or now_local()
    current = active_occurrence(now)
    following = next_occurrence(now)
    return {
        "current": current.to_dict() if current else None,
        "next": following.to_dict() if following else None,
        "has_schedule": bool(occurrences(now - timedelta(days=1), now + timedelta(days=1))),
    }
