"""Shared pytest fixtures for rtt-alhuda (FastAPI)."""

from __future__ import annotations

import asyncio

import pytest

from rtt_alhuda.config import validate_auth_config
from rtt_alhuda.database import dispose_engine


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Isolate the database and required auth env vars per test."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("KHUTBA_JWT_SECRET", "test-jwt-secret-min-32-characters-long")
    monkeypatch.setenv("KHUTBA_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("KHUTBA_ADMIN_PASSWORD", "changeme123")
    monkeypatch.setenv("KHUTBA_DEFAULT_ORG_SLUG", "default")

    # Reset any engine created by a previous test so it picks up the new URL.
    asyncio.run(dispose_engine())

    validate_auth_config()
    yield
    asyncio.run(dispose_engine())
