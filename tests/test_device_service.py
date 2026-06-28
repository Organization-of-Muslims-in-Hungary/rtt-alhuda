"""Tests for device_service helpers."""

from __future__ import annotations

import uuid

import pytest

from rtt_alhuda.database import get_engine, get_session_factory
from rtt_alhuda.db_models import Base, Organization
from rtt_alhuda.device_service import (
    delete_device,
    list_devices,
    register_device,
    rename_device,
)


@pytest.fixture(autouse=True)
async def _create_tables():
    """Ensure tables exist for device_service tests (no app lifespan in these)."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


async def _make_org() -> uuid.UUID:
    factory = get_session_factory()
    async with factory() as db:
        org = Organization(name="Test", slug=f"test-{uuid.uuid4().hex[:8]}")
        db.add(org)
        await db.commit()
        await db.refresh(org)
        return org.id


@pytest.mark.asyncio
async def test_register_creates_device():
    org_id = await _make_org()
    factory = get_session_factory()
    async with factory() as db:
        device = await register_device(
            db, org_id, None, "TV", 1920, 1080, "ua"
        )
    assert device["name"] == "TV"
    assert device["device_type"] == "screen"
    assert device["id"]


@pytest.mark.asyncio
async def test_register_reidentifies_existing():
    org_id = await _make_org()
    factory = get_session_factory()
    async with factory() as db:
        d1 = await register_device(db, org_id, None, "Old", 800, 600, "v1")
    async with factory() as db:
        d2 = await register_device(db, org_id, d1["id"], "New", 1920, 1080, "v2")
    assert d2["id"] == d1["id"]
    assert d2["name"] == "New"
    assert d2["device_type"] == "screen"


@pytest.mark.asyncio
async def test_register_empty_name_preserves_renamed():
    org_id = await _make_org()
    factory = get_session_factory()
    async with factory() as db:
        d = await register_device(db, org_id, None, "", 1920, 1080, "")
    async with factory() as db:
        await rename_device(db, org_id, d["id"], "Living Room TV")
    async with factory() as db:
        updated = await register_device(db, org_id, d["id"], "", 1920, 1080, "")
    assert updated["name"] == "Living Room TV"


@pytest.mark.asyncio
async def test_list_and_delete():
    org_id = await _make_org()
    factory = get_session_factory()
    async with factory() as db:
        await register_device(db, org_id, None, "A", 100, 100, "")
    async with factory() as db:
        devices = await list_devices(db, org_id)
    assert len(devices) == 1
    device_id = devices[0]["id"]
    async with factory() as db:
        ok = await delete_device(db, org_id, device_id)
    assert ok is True
    async with factory() as db:
        assert await list_devices(db, org_id) == []
