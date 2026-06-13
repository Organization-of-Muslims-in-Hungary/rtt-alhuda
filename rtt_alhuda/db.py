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
    # Future migrations go here:
    # (2, ["ALTER TABLE clients ADD COLUMN ...", "CREATE TABLE ..."]),
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
            # Only overwrite name if the caller actually provided one;
            # an empty string from an SSE reconnect should not erase a
            # name that was set via /api/clients/{id}/rename.
            if name:
                await db.execute(
                    """UPDATE clients
                       SET name = ?, device_type = ?, screen_w = ?, screen_h = ?,
                           last_seen = ?, user_agent = ?
                       WHERE id = ?""",
                    (name, device_type, screen_w, screen_h, now, user_agent, client_id),
                )
            else:
                await db.execute(
                    """UPDATE clients
                       SET device_type = ?, screen_w = ?, screen_h = ?,
                           last_seen = ?, user_agent = ?
                       WHERE id = ?""",
                    (device_type, screen_w, screen_h, now, user_agent, client_id),
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