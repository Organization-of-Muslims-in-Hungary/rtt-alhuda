"""Shared pytest fixtures for rtt-alhuda."""

import pytest

from rtt_alhuda.config import validate_auth_config


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch):
    """Required auth env vars for tests (fail-closed in production)."""
    monkeypatch.setenv(
        "KHUTBA_JWT_SECRET",
        "test-jwt-secret-min-32-characters-long",
    )
    monkeypatch.setenv("KHUTBA_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("KHUTBA_ADMIN_PASSWORD", "changeme")
    validate_auth_config()
