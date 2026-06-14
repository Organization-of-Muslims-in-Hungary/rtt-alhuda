"""Unit tests for rtt_alhuda.db — migrations and client CRUD."""

import time

import aiosqlite
import pytest

from rtt_alhuda import db as client_db


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _memory_db() -> aiosqlite.Connection:
    """Return an in-memory DB with migrations applied."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await client_db._migrate(conn)
    return conn


# ── Migration / schema tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fresh_db_starts_at_version_zero() -> None:
    conn = await aiosqlite.connect(":memory:")
    assert await client_db._get_schema_version(conn) == 0
    await conn.close()


@pytest.mark.asyncio
async def test_migrate_sets_latest_version() -> None:
    db = await _memory_db()
    version = await client_db._get_schema_version(db)
    assert version == client_db.LATEST_VERSION
    await db.close()


@pytest.mark.asyncio
async def test_migrate_creates_clients_table() -> None:
    db = await _memory_db()
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='clients'"
    )
    assert await cursor.fetchone() is not None
    await db.close()


@pytest.mark.asyncio
async def test_migrate_creates_meta_table() -> None:
    db = await _memory_db()
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='_meta'"
    )
    assert await cursor.fetchone() is not None
    await db.close()


@pytest.mark.asyncio
async def test_migrate_is_idempotent() -> None:
    """Running _migrate twice must not fail or change the version."""
    db = await _memory_db()
    await client_db._migrate(db)  # second run
    version = await client_db._get_schema_version(db)
    assert version == client_db.LATEST_VERSION
    await db.close()


@pytest.mark.asyncio
async def test_migrate_skips_already_applied() -> None:
    """If schema is already at LATEST_VERSION, _migrate does nothing."""
    db = await _memory_db()
    # Manually bump version beyond latest
    await client_db._set_schema_version(db, client_db.LATEST_VERSION + 10)
    await db.commit()
    await client_db._migrate(db)
    version = await client_db._get_schema_version(db)
    assert version == client_db.LATEST_VERSION + 10
    await db.close()


# ── Client CRUD tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_migrate_creates_users_table() -> None:
    db = await _memory_db()
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
    )
    assert await cursor.fetchone() is not None
    await db.close()


@pytest.mark.asyncio
async def test_seed_default_admin_only_once() -> None:
    db = await _memory_db()
    from rtt_alhuda.auth import hash_password
    from rtt_alhuda.config import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME

    first = await client_db.seed_default_admin(
        db, DEFAULT_ADMIN_USERNAME, hash_password(DEFAULT_ADMIN_PASSWORD)
    )
    second = await client_db.seed_default_admin(
        db, DEFAULT_ADMIN_USERNAME, hash_password(DEFAULT_ADMIN_PASSWORD)
    )
    assert first is not None
    assert second is None
    assert await client_db.count_users(db) == 1
    await db.close()


@pytest.mark.asyncio
async def test_register_new_client_generates_id() -> None:
    db = await _memory_db()
    client = await client_db.register_client(
        db, client_id=None, name="TV", screen_w=1920, screen_h=1080, user_agent="Test"
    )
    assert client["id"]  # non-empty
    assert len(client["id"]) == 12
    assert client["name"] == "TV"
    assert client["device_type"] == "screen"
    await db.close()


@pytest.mark.asyncio
async def test_register_with_explicit_id() -> None:
    db = await _memory_db()
    client = await client_db.register_client(
        db, client_id="my-custom-id", name="Phone", screen_w=390, screen_h=844, user_agent=""
    )
    assert client["id"] == "my-custom-id"
    assert client["device_type"] == "phone"
    await db.close()


@pytest.mark.asyncio
async def test_register_existing_client_updates() -> None:
    db = await _memory_db()
    c1 = await client_db.register_client(
        db, client_id=None, name="Old", screen_w=800, screen_h=600, user_agent="v1"
    )
    cid = c1["id"]
    first_seen = c1["first_seen"]

    c2 = await client_db.register_client(
        db, client_id=cid, name="New", screen_w=1920, screen_h=1080, user_agent="v2"
    )
    assert c2["id"] == cid
    assert c2["name"] == "New"
    assert c2["device_type"] == "screen"
    assert c2["user_agent"] == "v2"
    assert c2["first_seen"] == first_seen  # unchanged
    assert c2["last_seen"] >= c1["last_seen"]
    await db.close()


@pytest.mark.asyncio
async def test_device_type_phone_threshold() -> None:
    db = await _memory_db()
    phone = await client_db.register_client(
        db, client_id=None, name="", screen_w=375, screen_h=812, user_agent=""
    )
    assert phone["device_type"] == "phone"

    screen = await client_db.register_client(
        db, client_id=None, name="", screen_w=1024, screen_h=768, user_agent=""
    )
    assert screen["device_type"] == "screen"
    await db.close()


@pytest.mark.asyncio
async def test_device_type_unknown_when_no_dimensions() -> None:
    db = await _memory_db()
    client = await client_db.register_client(
        db, client_id=None, name="", screen_w=0, screen_h=0, user_agent=""
    )
    assert client["device_type"] == "unknown"
    await db.close()


@pytest.mark.asyncio
async def test_touch_client_updates_last_seen() -> None:
    db = await _memory_db()
    client = await client_db.register_client(
        db, client_id=None, name="X", screen_w=100, screen_h=100, user_agent=""
    )
    old_ts = client["last_seen"]
    await client_db.touch_client(db, client["id"])
    updated = await client_db._get_client(db, client["id"])
    assert updated["last_seen"] >= old_ts
    await db.close()


@pytest.mark.asyncio
async def test_rename_client() -> None:
    db = await _memory_db()
    client = await client_db.register_client(
        db, client_id=None, name="Old", screen_w=100, screen_h=100, user_agent=""
    )
    ok = await client_db.rename_client(db, client["id"], "Renamed")
    assert ok is True
    updated = await client_db._get_client(db, client["id"])
    assert updated["name"] == "Renamed"
    await db.close()


@pytest.mark.asyncio
async def test_rename_nonexistent_client_returns_false() -> None:
    db = await _memory_db()
    ok = await client_db.rename_client(db, "does-not-exist", "Name")
    assert ok is False
    await db.close()


@pytest.mark.asyncio
async def test_list_clients_ordered_by_last_seen() -> None:
    db = await _memory_db()
    await client_db.register_client(
        db, client_id="aaa", name="First", screen_w=100, screen_h=100, user_agent=""
    )
    await client_db.register_client(
        db, client_id="bbb", name="Second", screen_w=100, screen_h=100, user_agent=""
    )
    # Touch "aaa" so it becomes most recent
    await client_db.touch_client(db, "aaa")
    clients = await client_db.list_clients(db)
    assert len(clients) == 2
    assert clients[0]["id"] == "aaa"
    assert clients[1]["id"] == "bbb"
    await db.close()


@pytest.mark.asyncio
async def test_list_clients_empty() -> None:
    db = await _memory_db()
    clients = await client_db.list_clients(db)
    assert clients == []
    await db.close()


@pytest.mark.asyncio
async def test_delete_client() -> None:
    db = await _memory_db()
    client = await client_db.register_client(
        db, client_id=None, name="Gone", screen_w=100, screen_h=100, user_agent=""
    )
    ok = await client_db.delete_client(db, client["id"])
    assert ok is True
    remaining = await client_db.list_clients(db)
    assert len(remaining) == 0
    await db.close()


@pytest.mark.asyncio
async def test_delete_nonexistent_returns_false() -> None:
    db = await _memory_db()
    ok = await client_db.delete_client(db, "nope")
    assert ok is False
    await db.close()


@pytest.mark.asyncio
async def test_get_client_nonexistent_returns_empty_dict() -> None:
    db = await _memory_db()
    result = await client_db._get_client(db, "nope")
    assert result == {}
    await db.close()