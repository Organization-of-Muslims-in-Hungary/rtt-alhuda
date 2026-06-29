"""Pydantic request/response schemas for the API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Auth ──────────────────────────────────────────────────────────────────────


class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    ok: bool = True
    token: str
    user: "UserPublic"


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    email: str
    role: str
    status: str
    created_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None


TokenResponse.model_rebuild()


# ── Organizations ─────────────────────────────────────────────────────────────


class OrgCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")


class OrgPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    created_at: Optional[datetime] = None


# ── Admin user management ─────────────────────────────────────────────────────


class UserCreate(BaseModel):
    email: str
    password: str = Field(min_length=8)
    role: str = Field(default="operator", pattern=r"^(admin|operator)$")


class UserStatusUpdate(BaseModel):
    status: str = Field(pattern=r"^(active|suspended|pending)$")


# ── Devices ───────────────────────────────────────────────────────────────────


class DeviceRegister(BaseModel):
    client_id: Optional[str] = None
    name: str = ""
    screen_w: int = 0
    screen_h: int = 0


class DeviceRename(BaseModel):
    name: str


class DevicePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    device_type: str
    screen_w: int
    screen_h: int
    user_agent: str
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    connected: bool = False
