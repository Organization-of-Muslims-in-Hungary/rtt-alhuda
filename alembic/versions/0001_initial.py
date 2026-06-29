"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-06-29

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False, unique=True),
        sa.Column("settings", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_organizations_slug", "organizations", ["slug"], unique=True)

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "role",
            sa.Enum("superadmin", "admin", "operator", name="role", native_enum=False, create_constraint=True),
            nullable=False,
            server_default="operator",
        ),
        sa.Column(
            "status",
            sa.Enum("pending", "active", "suspended", name="userstatus", native_enum=False, create_constraint=True),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_org_id", "users", ["org_id"])
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "devices",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False, server_default=""),
        sa.Column(
            "device_type",
            sa.Enum("phone", "screen", "unknown", name="devicetype", native_enum=False, create_constraint=True),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("screen_w", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("screen_h", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("user_agent", sa.Text(), nullable=False, server_default=""),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_devices_org_id", "devices", ["org_id"])

    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "audio_source",
            sa.Enum("internal", "remote", name="audiosource", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("active", "completed", "error", name="sessionstatus", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_sessions_org_id", "sessions", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_sessions_org_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_devices_org_id", table_name="devices")
    op.drop_table("devices")
    op.drop_index("ix_users_org_id", table_name="users")
    op.drop_table("users")
    op.drop_index("ix_organizations_slug", table_name="organizations")
    op.drop_table("organizations")
