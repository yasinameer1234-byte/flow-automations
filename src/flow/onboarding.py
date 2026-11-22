"""Create a real client business from a profile file, without touching code.

    flow create-tenant config/clients/example-plumbing.toml

The file holds only business facts the owner gave us (hours, services, approved answers,
staff, emergency rules). It is validated before anything is written, everything is written
in one transaction, and the owner's password is typed at a hidden prompt, never stored in
the file. Real businesses are never marked as demo, so they get no public demo page.
"""

from __future__ import annotations

import json
import re
import secrets
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

from .core.db import Database
from .core.models import utcnow
from .revenue.engine import RevenueEngine
from .secretary.schema import PACKAGES
from .secretary.seed import api_key_hash
from .secretary.store import Store
from .secretary.tools import normalize_phone

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class Business(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,40}$")
    name: str = Field(min_length=2, max_length=120)
    package: str
    timezone: str
    business_phone: str
    review_url: str | None = None
    avg_job_value: float = Field(gt=0)
    close_rate: float = Field(gt=0, le=1)

    @field_validator("package")
    @classmethod
    def _package(cls, v: str) -> str:
        if v not in PACKAGES:
            raise ValueError(f"package must be one of {', '.join(PACKAGES)}")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as e:
            raise ValueError(f"unknown timezone {v!r}") from e
        return v

    @field_validator("business_phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        p = normalize_phone(v)
        if not p:
            raise ValueError("business_phone must be a 10-digit North American number")
        return p

    @field_validator("review_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v and not v.startswith("https://"):
            raise ValueError("review_url must start with https://")
        return v


class AI(BaseModel):
    disclosure: str = Field(min_length=10)
    emergency_keywords: list[str] = Field(min_length=1)
    emergency_instructions: str = Field(min_length=10)


class Service(BaseModel):
    name: str
    keywords: list[str] = Field(min_length=1)
    duration_minutes: int = Field(ge=15, le=480)
    bookable: bool = True


class Answer(BaseModel):
    question: str
    keywords: list[str] = Field(min_length=1)
    answer: str = Field(min_length=3)
    approved: bool = False  # only owner-approved answers are ever spoken


class Staff(BaseModel):
    name: str
    phone: str
    handles_emergencies: bool = False
    available: bool = True

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        p = normalize_phone(v)
        if not p:
            raise ValueError("staff phone must be a 10-digit number")
        return p


class Owner(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    name: str


class ClientProfile(BaseModel):
    business: Business
    hours: dict[str, list[str]]
    ai: AI
    services: list[Service] = Field(min_length=1)
    answers: list[Answer] = []
    staff: list[Staff] = []
    owner: Owner

    @field_validator("hours")
    @classmethod
    def _hours(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for day, span in v.items():
            if day not in DAYS:
                raise ValueError(f"unknown day {day!r}; use {', '.join(DAYS)}")
            if len(span) != 2 or not all(_HHMM.match(x) for x in span) or span[0] >= span[1]:
                raise ValueError(f'{day}: hours must be ["HH:MM", "HH:MM"], open before close')
        if not v:
            raise ValueError("at least one day of opening hours is required")
        return v

    @model_validator(mode="after")
    def _bookable(self) -> ClientProfile:
        if not any(s.bookable for s in self.services):
            raise ValueError("at least one service must be bookable")
        return self


def load_profile(path: str | Path) -> ClientProfile:
    return ClientProfile.model_validate(tomllib.loads(Path(path).read_text()))


class OnboardingError(Exception):
    pass


def create_tenant(db: Database, p: ClientProfile, *, owner_password_hash: str) -> dict:
    """Write the business, its profile and its owner. Returns ids and a one-time tool key."""
    store = Store(db)
    RevenueEngine(db, store)  # make sure revenue tables exist
    b = p.business
    if store.tenant_by_slug(b.slug):
        raise OnboardingError(f"a business with slug {b.slug!r} already exists")
    if db.one("SELECT 1 FROM users WHERE email = ?", (p.owner.email.lower(),)):
        raise OnboardingError(f"a user with email {p.owner.email} already exists")
    tid = "t_" + b.slug.replace("-", "_")
    tool_key = secrets.token_urlsafe(32)
    now = utcnow()
    with db.transaction():
        db.execute(
            "INSERT INTO tenants (id, slug, name, package, api_key_hash, is_demo, created_at) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (tid, b.slug, b.name, b.package, api_key_hash(tool_key), now),
        )
        db.execute(
            "INSERT INTO business_profiles (tenant_id, timezone, hours_json, disclosure, "
            "recording_enabled, emergency_keywords_json, emergency_instructions, avg_job_value,"
            " est_close_rate, minutes_saved_per_call) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, 4)",
            (
                tid,
                b.timezone,
                json.dumps(p.hours),
                p.ai.disclosure,
                json.dumps([k.lower() for k in p.ai.emergency_keywords]),
                p.ai.emergency_instructions,
                b.avg_job_value,
                b.close_rate,
            ),
        )
        for s in p.services:
            db.execute(
                "INSERT INTO services (tenant_id, name, keywords_json, duration_minutes, bookable)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    tid,
                    s.name,
                    json.dumps([k.lower() for k in s.keywords]),
                    s.duration_minutes,
                    int(s.bookable),
                ),
            )
        for a in p.answers:
            db.execute(
                "INSERT INTO knowledge_entries (tenant_id, question, keywords_json, answer, "
                "approved) VALUES (?, ?, ?, ?, ?)",
                (
                    tid,
                    a.question,
                    json.dumps([k.lower() for k in a.keywords]),
                    a.answer,
                    int(a.approved),
                ),
            )
        for st in p.staff:
            db.execute(
                "INSERT INTO staff_members (tenant_id, name, phone, handles_emergencies, "
                "available) VALUES (?, ?, ?, ?, ?)",
                (tid, st.name, st.phone, int(st.handles_emergencies), int(st.available)),
            )
        db.execute(
            "INSERT INTO users (email, name, password_hash, role, tenant_id, created_at) "
            "VALUES (?, ?, ?, 'owner', ?, ?)",
            (p.owner.email.lower(), p.owner.name, owner_password_hash, tid, now),
        )
        db.execute(
            "INSERT INTO revenue_settings (tenant_id, business_phone, review_url) VALUES (?,?,?)",
            (tid, b.business_phone, b.review_url),
        )
    return {"tenant_id": tid, "slug": b.slug, "tool_key": tool_key}
