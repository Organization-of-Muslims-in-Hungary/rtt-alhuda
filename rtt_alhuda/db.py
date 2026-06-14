"""SQLite database: versioned schema with client registry (and future tables)."""

import time
import uuid
from typing import Optional

import aiosqlite

from rtt_alhuda.config import REPO_ROOT

DB_PATH = REPO_ROOT / "alhuda.db"

# ── Schema migrations ─────────────────────────────────────────────────────────
# Each entry is (version, list_of_sql_statements).
# Migrations run in order; only those newer than the stored version execute.

MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, [
        """CREATE TABLE IF NOT EXISTS clients (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL DEFAULT '',
            device_type TEXT NOT NULL DEFAULT 'unknown',
            screen_w    INTEGER NOT NULL DEFAULT 0,
            screen_h    INTEGER NOT NULL DEFAULT 0,
            first_seen  REAL NOT NULL,
            last_seen   REAL NOT NULL,
            user_agent  TEXT NOT NULL DEFAULT ''
        );""",
    ]),
    (2, [
        """CREATE TABLE IF NOT EXISTS users (
            id            TEXT PRIMARY KEY,
            username      TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'operator',
            status        TEXT NOT NULL DEFAULT 'pending',
            created_at    REAL NOT NULL,
            approved_at   REAL,
            approved_by   TEXT
        );""",
    ]),
    (3, [
        """CREATE TABLE users_new (
            id            TEXT PRIMARY KEY,
            username      TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'operator'
                CHECK (role IN ('admin', 'operator')),
            status        TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('approved', 'pending', 'rejected')),
            created_at    REAL NOT NULL,
            approved_at   REAL,
            approved_by   TEXT
        );""",
        "INSERT INTO users_new SELECT * FROM users;",
        "DROP TABLE users;",
        "ALTER TABLE users_new RENAME TO users;",
    ]),
]

LATEST_VERSION = MIGRATIONS[-1][0]


async def _get_schema_version(db: aiosqlite.Connection) -> int:
    """Return the current schema version (0 if brand-new database)."""
    # The meta table may not exist yet on a fresh DB.
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='_meta'"
    )
    if not await cursor.fetchone():
        return 0
    cursor = await db.execute("SELECT value FROM _meta WHERE key = 'schema_version'")
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def _set_schema_version(db: aiosqlite.Connection, version: int) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
        (str(version),),
    )


async def _migrate(db: aiosqlite.Connection) -> None:
    """Run any outstanding migrations."""
    current = await _get_schema_version(db)

    if current >= LATEST_VERSION:
        return

    # Ensure the _meta table exists before the first migration.
    await db.execute(
        "CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )

    for version, statements in MIGRATIONS:
        if version <= current:
            continue
        for sql in statements:
            await db.execute(sql)
        await _set_schema_version(db, version)

    await db.commit()


async def get_db() -> aiosqlite.Connection:
    """Open (or create) the database and run pending migrations."""
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await _migrate(db)
    return db


async def register_client(
    db: aiosqlite.Connection,
    client_id: Optional[str],
    name: str,
    screen_w: int,
    screen_h: int,
    user_agent: str,
) -> dict:
    """Register a new client or update an existing one.  Returns the client row as dict."""
    now = time.time()

    # Determine device type from screen dimensions
    if screen_w and screen_h:
        device_type = "phone" if max(screen_w, screen_h) < 1024 else "screen"
    else:
        device_type = "unknown"

    if client_id:
        # Check if existing
        cursor = await db.execute("SELECT id FROM clients WHERE id = ?", (client_id,))
        row = await cursor.fetchone()
        if row:
            await db.execute(
                """UPDATE clients
                   SET name = ?, device_type = ?, screen_w = ?, screen_h = ?,
                       last_seen = ?, user_agent = ?
                   WHERE id = ?""",
                (name, device_type, screen_w, screen_h, now, user_agent, client_id),
            )
            await db.commit()
            return await _get_client(db, client_id)

    # New client
    new_id = client_id or uuid.uuid4().hex[:12]
    await db.execute(
        """INSERT INTO clients (id, name, device_type, screen_w, screen_h, first_seen, last_seen, user_agent)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (new_id, name, device_type, screen_w, screen_h, now, now, user_agent),
    )
    await db.commit()
    return await _get_client(db, new_id)


async def touch_client(db: aiosqlite.Connection, client_id: str) -> None:
    """Update last_seen timestamp."""
    await db.execute(
        "UPDATE clients SET last_seen = ? WHERE id = ?", (time.time(), client_id)
    )
    await db.commit()


async def rename_client(db: aiosqlite.Connection, client_id: str, name: str) -> bool:
    """Rename a client.  Returns True if the client existed."""
    cursor = await db.execute(
        "UPDATE clients SET name = ? WHERE id = ?", (name, client_id)
    )
    await db.commit()
    return cursor.rowcount > 0


async def list_clients(db: aiosqlite.Connection) -> list[dict]:
    """Return all known clients ordered by last_seen desc."""
    cursor = await db.execute("SELECT * FROM clients ORDER BY last_seen DESC")
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def delete_client(db: aiosqlite.Connection, client_id: str) -> bool:
    cursor = await db.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    await db.commit()
    return cursor.rowcount > 0


async def _get_client(db: aiosqlite.Connection, client_id: str) -> dict:
    cursor = await db.execute("SELECT * FROM clients WHERE id = ?", (client_id,))
    row = await cursor.fetchone()
    return dict(row) if row else {}


# ── Operator user accounts ────────────────────────────────────────────────────

VALID_USER_ROLES = frozenset({"admin", "operator"})
VALID_USER_STATUSES = frozenset({"approved", "pending", "rejected"})


class InvalidUserField(ValueError):
    """Raised when role or status is outside the allowed vocabulary."""


def _validate_user_role(role: str) -> None:
    if role not in VALID_USER_ROLES:
        raise InvalidUserField(f"invalid role: {role}")


def _validate_user_status(status: str) -> None:
    if status not in VALID_USER_STATUSES:
        raise InvalidUserField(f"invalid status: {status}")


async def count_users(db: aiosqlite.Connection) -> int:
    cursor = await db.execute("SELECT COUNT(*) FROM users")
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def create_user(
    db: aiosqlite.Connection,
    username: str,
    password_hash: str,
    *,
    role: str = "operator",
    status: str = "pending",
    approved_at: Optional[float] = None,
    approved_by: Optional[str] = None,
) -> dict:
    """Insert a new operator account and return the full row."""
    _validate_user_role(role)
    _validate_user_status(status)
    user_id = uuid.uuid4().hex
    now = time.time()
    await db.execute(
        """INSERT INTO users
           (id, username, password_hash, role, status, created_at, approved_at, approved_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, username, password_hash, role, status, now, approved_at, approved_by),
    )
    await db.commit()
    return await get_user_by_id(db, user_id)


async def get_user_by_id(db: aiosqlite.Connection, user_id: str) -> dict:
    cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = await cursor.fetchone()
    return dict(row) if row else {}


async def get_user_by_username(db: aiosqlite.Connection, username: str) -> dict:
    cursor = await db.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else {}


async def list_users(db: aiosqlite.Connection) -> list[dict]:
    cursor = await db.execute("SELECT * FROM users ORDER BY created_at DESC")
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def set_user_status(
    db: aiosqlite.Connection,
    user_id: str,
    status: str,
    *,
    approved_by: Optional[str] = None,
) -> bool:
    """Update approval status. Returns False if the user does not exist."""
    _validate_user_status(status)
    approved_at = time.time() if status == "approved" else None
    cursor = await db.execute(
        """UPDATE users
           SET status = ?, approved_at = ?, approved_by = ?
           WHERE id = ?""",
        (status, approved_at, approved_by, user_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def delete_user(db: aiosqlite.Connection, user_id: str) -> bool:
    cursor = await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    await db.commit()
    return cursor.rowcount > 0


async def seed_default_admin(
    db: aiosqlite.Connection,
    username: str,
    password_hash: str,
) -> Optional[dict]:
    """Create the default admin account when the users table is empty."""
    if await count_users(db) > 0:
        return None
    return await create_user(
        db,
        username,
        password_hash,
        role="admin",
        status="approved",
        approved_at=time.time(),
    )