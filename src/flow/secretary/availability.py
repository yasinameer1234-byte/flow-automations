"""Appointment availability: business hours minus existing bookings.

This is the ONLY source of times the agent may offer. Later, busy times from the
client's Google/Microsoft calendar are subtracted here too.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..core.db import Database
from .store import BusinessProfile

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass(frozen=True)
class Slot:
    start: datetime  # aware, UTC
    end: datetime
    tz: str

    @property
    def id(self) -> str:
        return self.start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def local(self) -> datetime:
        return self.start.astimezone(ZoneInfo(self.tz))

    def label(self) -> str:
        d = self.local()
        hour = d.strftime("%I:%M %p").lstrip("0")
        return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day} at {hour}"


def parse_slot_id(slot_id: str) -> datetime:
    return datetime.strptime(slot_id, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def is_open(profile: BusinessProfile, now: datetime) -> bool:
    local = now.astimezone(ZoneInfo(profile.timezone))
    hours = profile.hours.get(DAYS[local.weekday()])
    if not hours:
        return False
    start, end = (time.fromisoformat(h) for h in hours)
    return start <= local.time() < end


def booked_starts(db: Database, tenant_id: str) -> set[str]:
    rows = db.query(
        "SELECT starts_at FROM appointments WHERE tenant_id = ? AND status = 'booked'", (tenant_id,)
    )
    return {r["starts_at"] for r in rows}


def available_slots(
    db: Database,
    profile: BusinessProfile,
    duration_minutes: int,
    now: datetime,
    *,
    on_date: date | None = None,
    part_of_day: str | None = None,
    limit: int = 3,
    step_minutes: int = 60,
) -> list[Slot]:
    tz = ZoneInfo(profile.timezone)
    earliest = now + timedelta(minutes=profile.booking_lead_minutes)
    taken = booked_starts(db, profile.tenant_id)
    first_day = now.astimezone(tz).date()
    out: list[Slot] = []
    for offset in range(profile.booking_horizon_days + 1):
        day = first_day + timedelta(days=offset)
        if on_date and day != on_date:
            continue
        hours = profile.hours.get(DAYS[day.weekday()])
        if not hours:
            continue
        open_t, close_t = (time.fromisoformat(h) for h in hours)
        cursor = datetime.combine(day, open_t, tzinfo=tz)
        close = datetime.combine(day, close_t, tzinfo=tz)
        while cursor + timedelta(minutes=duration_minutes) <= close:
            slot = Slot(
                cursor.astimezone(UTC),
                (cursor + timedelta(minutes=duration_minutes)).astimezone(UTC),
                profile.timezone,
            )
            if slot.start >= earliest and slot.id not in taken and _part_ok(cursor, part_of_day):
                out.append(slot)
                if len(out) >= limit:
                    return out
            cursor += timedelta(minutes=step_minutes)
    return out


def _part_ok(local_start: datetime, part: str | None) -> bool:
    if not part:
        return True
    h = local_start.hour
    return {"morning": h < 12, "afternoon": 12 <= h < 17, "evening": h >= 17}.get(part, True)
