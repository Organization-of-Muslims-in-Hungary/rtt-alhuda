"""SQLAlchemy ORM models for the multi-tenant service."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)
from sqlalchemy.types import JSON


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_type():
    """Use JSONB on Postgres, plain JSON elsewhere (SQLite)."""
    from rtt_alhuda.config import database_url

    if database_url().startswith("postgresql"):
        return JSONB()
    return JSON()


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class Role(str, enum.Enum):
    superadmin = "superadmin"
    admin = "admin"
    operator = "operator"


class UserStatus(str, enum.Enum):
    pending = "pending"
    active = "active"
    suspended = "suspended"


class DeviceType(str, enum.Enum):
    phone = "phone"
    screen = "screen"
    unknown = "unknown"


class AudioSource(str, enum.Enum):
    internal = "internal"
    remote = "remote"


class SessionStatus(str, enum.Enum):
    active = "active"
    completed = "completed"
    error = "error"


def _enum(enum_cls: type[enum.Enum]):
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=True,
        length=20,
    )


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    settings: Mapped[dict] = mapped_column(_json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
    )

    users: Mapped[list["User"]] = relationship(
        back_populates="org",
        cascade="all, delete-orphan",
    )
    devices: Mapped[list["Device"]] = relationship(
        back_populates="org",
        cascade="all, delete-orphan",
    )
    sessions: Mapped[list["SessionRecord"]] = relationship(
        back_populates="org",
        cascade="all, delete-orphan",
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        index=True,
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[Role] = mapped_column(_enum(Role), default=Role.operator)
    status: Mapped[UserStatus] = mapped_column(
        _enum(UserStatus),
        default=UserStatus.pending,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    org: Mapped["Organization"] = relationship(back_populates="users")


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(120), default="")
    device_type: Mapped[DeviceType] = mapped_column(
        _enum(DeviceType),
        default=DeviceType.unknown,
    )
    screen_w: Mapped[int] = mapped_column(Integer, default=0)
    screen_h: Mapped[int] = mapped_column(Integer, default=0)
    user_agent: Mapped[str] = mapped_column(Text, default="")
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
    )

    org: Mapped["Organization"] = relationship(back_populates="devices")


class SessionRecord(Base):
    """Historical recording session row (defined now, persisted later)."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        index=True,
    )
    audio_source: Mapped[AudioSource] = mapped_column(_enum(AudioSource))
    status: Mapped[SessionStatus] = mapped_column(_enum(SessionStatus))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
    )
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    org: Mapped["Organization"] = relationship(back_populates="sessions")
