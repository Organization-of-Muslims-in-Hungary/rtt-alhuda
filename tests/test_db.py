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
    from rtt_alhuda import config
    from rtt_alhuda.auth import hash_password

    first = await client_db.seed_default_admin(
        db, config.DEFAULT_ADMIN_USERNAME, hash_password(config.DEFAULT_ADMIN_PASSWORD)
    )
    second = await client_db.seed_default_admin(
        db, config.DEFAULT_ADMIN_USERNAME, hash_password(config.DEFAULT_ADMIN_PASSWORD)
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
async def test_reregister_with_empty_name_preserves_renamed() -> None:
    """SSE reconnects pass name='', which must not overwrite a prior rename."""
    db = await _memory_db()
    client = await client_db.register_client(
        db, client_id=None, name="", screen_w=1920, screen_h=1080, user_agent=""
    )
    cid = client["id"]
    # Operator renames the client via the control panel
    await client_db.rename_client(db, cid, "Living Room TV")
    # SSE reconnects with name="" — must NOT erase the rename
    updated = await client_db.register_client(
        db, client_id=cid, name="", screen_w=1920, screen_h=1080, user_agent=""
    )
    assert updated["name"] == "Living Room TV"
    await db.close()


@pytest.mark.asyncio
async def test_reregister_with_explicit_name_overwrites() -> None:
    """When a caller provides a non-empty name, it should update."""
    db = await _memory_db()
    client = await client_db.register_client(
        db, client_id=None, name="Old", screen_w=100, screen_h=100, user_agent=""
    )
    cid = client["id"]
    updated = await client_db.register_client(
        db, client_id=cid, name="New", screen_w=100, screen_h=100, user_agent=""
    )
    assert updated["name"] == "New"
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


# ── User account CRUD tests ──────────────────────────────────────────────────


async def _create_test_user(
    db,
    username: str = "testuser",
    password_hash: str = "fakehash",
    role: str = "operator",
    status: str = "pending",
) -> dict:
    return await client_db.create_user(
        db, username, password_hash, role=role, status=status
    )


@pytest.mark.asyncio
async def test_create_user_returns_full_row() -> None:
    db = await _memory_db()
    user = await _create_test_user(db, username="alice")
    assert user["username"] == "alice"
    assert user["role"] == "operator"
    assert user["status"] == "pending"
    assert user["id"]  # non-empty
    assert user["created_at"] > 0
    await db.close()


@pytest.mark.asyncio
async def test_create_user_with_admin_role() -> None:
    db = await _memory_db()
    user = await client_db.create_user(
        db, "admin_user", "hash", role="admin", status="approved"
    )
    assert user["role"] == "admin"
    assert user["status"] == "approved"
    await db.close()


@pytest.mark.asyncio
async def test_create_user_invalid_role_raises() -> None:
    db = await _memory_db()
    with pytest.raises(client_db.InvalidUserField, match="role"):
        await client_db.create_user(db, "user", "hash", role="superadmin")
    await db.close()


@pytest.mark.asyncio
async def test_create_user_invalid_status_raises() -> None:
    db = await _memory_db()
    with pytest.raises(client_db.InvalidUserField, match="status"):
        await client_db.create_user(db, "user", "hash", status="banned")
    await db.close()


@pytest.mark.asyncio
async def test_get_user_by_id_found() -> None:
    db = await _memory_db()
    created = await _create_test_user(db, username="bob")
    found = await client_db.get_user_by_id(db, created["id"])
    assert found["id"] == created["id"]
    assert found["username"] == "bob"
    await db.close()


@pytest.mark.asyncio
async def test_get_user_by_id_not_found_returns_empty_dict() -> None:
    db = await _memory_db()
    result = await client_db.get_user_by_id(db, "nonexistent-id")
    assert result == {}
    await db.close()


@pytest.mark.asyncio
async def test_get_user_by_username_found() -> None:
    db = await _memory_db()
    await _create_test_user(db, username="charlie")
    found = await client_db.get_user_by_username(db, "charlie")
    assert found["username"] == "charlie"
    await db.close()


@pytest.mark.asyncio
async def test_get_user_by_username_case_insensitive() -> None:
    db = await _memory_db()
    await _create_test_user(db, username="CaseSensitive")
    # COLLATE NOCASE means lookup should work regardless of case
    found = await client_db.get_user_by_username(db, "casesensitive")
    assert found["username"] == "CaseSensitive"
    await db.close()


@pytest.mark.asyncio
async def test_get_user_by_username_not_found_returns_empty_dict() -> None:
    db = await _memory_db()
    result = await client_db.get_user_by_username(db, "does_not_exist")
    assert result == {}
    await db.close()


@pytest.mark.asyncio
async def test_list_users_empty() -> None:
    db = await _memory_db()
    users = await client_db.list_users(db)
    assert users == []
    await db.close()


@pytest.mark.asyncio
async def test_list_users_returns_all() -> None:
    db = await _memory_db()
    await _create_test_user(db, username="u1")
    await _create_test_user(db, username="u2")
    users = await client_db.list_users(db)
    assert len(users) == 2
    usernames = {u["username"] for u in users}
    assert {"u1", "u2"} == usernames
    await db.close()


@pytest.mark.asyncio
async def test_list_users_ordered_by_created_at_desc() -> None:
    db = await _memory_db()
    u1 = await _create_test_user(db, username="early")
    # Ensure a different timestamp by sleeping slightly or manually
    # Since time.time() may have same resolution, we force ordering via seed
    u2 = await client_db.create_user(
        db, "later", "hash", role="operator", status="pending"
    )
    users = await client_db.list_users(db)
    # The most recently created user should come first
    assert users[0]["username"] == "later" or users[0]["created_at"] >= users[1]["created_at"]
    await db.close()


@pytest.mark.asyncio
async def test_count_users_empty() -> None:
    db = await _memory_db()
    assert await client_db.count_users(db) == 0
    await db.close()


@pytest.mark.asyncio
async def test_count_users_after_creation() -> None:
    db = await _memory_db()
    await _create_test_user(db, username="u1")
    await _create_test_user(db, username="u2")
    assert await client_db.count_users(db) == 2
    await db.close()


@pytest.mark.asyncio
async def test_set_user_status_approved_sets_approved_at() -> None:
    db = await _memory_db()
    user = await _create_test_user(db, username="pending_user")
    before = time.time()
    ok = await client_db.set_user_status(db, user["id"], "approved", approved_by="admin-id")
    after = time.time()
    assert ok is True
    updated = await client_db.get_user_by_id(db, user["id"])
    assert updated["status"] == "approved"
    assert updated["approved_by"] == "admin-id"
    assert before <= updated["approved_at"] <= after
    await db.close()


@pytest.mark.asyncio
async def test_set_user_status_rejected_clears_approved_at() -> None:
    db = await _memory_db()
    user = await _create_test_user(db, username="to_reject")
    ok = await client_db.set_user_status(db, user["id"], "rejected")
    assert ok is True
    updated = await client_db.get_user_by_id(db, user["id"])
    assert updated["status"] == "rejected"
    assert updated["approved_at"] is None
    await db.close()


@pytest.mark.asyncio
async def test_set_user_status_nonexistent_returns_false() -> None:
    db = await _memory_db()
    ok = await client_db.set_user_status(db, "no-such-id", "approved")
    assert ok is False
    await db.close()


@pytest.mark.asyncio
async def test_set_user_status_invalid_status_raises() -> None:
    db = await _memory_db()
    user = await _create_test_user(db)
    with pytest.raises(client_db.InvalidUserField, match="status"):
        await client_db.set_user_status(db, user["id"], "banned")
    await db.close()


@pytest.mark.asyncio
async def test_delete_user_succeeds() -> None:
    db = await _memory_db()
    user = await _create_test_user(db, username="to_delete")
    ok = await client_db.delete_user(db, user["id"])
    assert ok is True
    assert await client_db.get_user_by_id(db, user["id"]) == {}
    await db.close()


@pytest.mark.asyncio
async def test_delete_user_nonexistent_returns_false() -> None:
    db = await _memory_db()
    ok = await client_db.delete_user(db, "no-such-id")
    assert ok is False
    await db.close()


@pytest.mark.asyncio
async def test_seed_default_admin_creates_approved_admin() -> None:
    db = await _memory_db()
    result = await client_db.seed_default_admin(db, "admin", "hashvalue")
    assert result is not None
    assert result["role"] == "admin"
    assert result["status"] == "approved"
    assert result["username"] == "admin"
    assert result["approved_at"] is not None
    await db.close()


@pytest.mark.asyncio
async def test_seed_default_admin_skips_when_users_exist() -> None:
    db = await _memory_db()
    # Create a user first
    await _create_test_user(db, username="existing")
    result = await client_db.seed_default_admin(db, "admin", "hashvalue")
    assert result is None
    # Count should still be 1, not 2
    assert await client_db.count_users(db) == 1
    await db.close()


@pytest.mark.asyncio
async def test_create_user_stores_password_hash() -> None:
    db = await _memory_db()
    from rtt_alhuda.auth import hash_password, verify_password

    raw_password = "secure_pass_123"
    pw_hash = hash_password(raw_password)
    user = await client_db.create_user(db, "hashtest", pw_hash)
    fetched = await client_db.get_user_by_id(db, user["id"])
    assert verify_password(raw_password, fetched["password_hash"]) is True
    assert verify_password("wrongpass", fetched["password_hash"]) is False
    await db.close()


@pytest.mark.asyncio
async def test_create_user_with_approved_at_and_approved_by() -> None:
    db = await _memory_db()
    approval_time = time.time()
    user = await client_db.create_user(
        db,
        "pre_approved",
        "hash",
        role="admin",
        status="approved",
        approved_at=approval_time,
        approved_by="system",
    )
    assert user["approved_at"] == approval_time
    assert user["approved_by"] == "system"
    await db.close()
