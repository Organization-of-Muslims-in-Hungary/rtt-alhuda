"""SQLite-backed client registry for per-client SSE control."""

import time
import uuid
from pathlib import Path
from typing import Optional

import aiosqlite

from rtt_alhuda.config import REPO_ROOT

DB_PATH = REPO_ROOT / "clients.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS clients (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    device_type TEXT NOT NULL DEFAULT 'unknown',
    screen_w    INTEGER NOT NULL DEFAULT 0,
    screen_h    INTEGER NOT NULL DEFAULT 0,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    user_agent  TEXT NOT NULL DEFAULT ''
);
"""


async def get_db() -> aiosqlite.Connection:
    """Open (or create) the clients database and ensure the table exists."""
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute(_CREATE_TABLE)
    await db.commit()
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