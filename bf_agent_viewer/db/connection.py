"""Database connection handling.

connect() is intentionally non-destructive -- it does NOT wipe an existing
database file. That distinction mattered in practice during prototyping
(Sept 2026): an earlier destructive init on every process start silently
discarded seeded identity data every time the gateway restarted. See
research.md, OQ-003 validation notes.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_MIGRATIONS_PATH = Path(__file__).with_name("migrations.sql")


def _migrate_agents_owner_nullable(conn: sqlite3.Connection) -> None:
    """T-012 (F-027): passive discovery needs to write a real `agents` row
    for an unrecognized caller before anyone has assigned it an owner --
    schema.sql's original `owner_id TEXT NOT NULL` made that impossible on
    a database created before this change. SQLite has no ALTER COLUMN to
    drop a NOT NULL constraint, so this rebuilds the table the standard
    SQLite way (new table in the post-migration shape, copy the rows,
    swap it in) -- but only when the table is still in the old shape, so
    this is a no-op on every process start after the first. Expressed in
    Python rather than added to migrations.sql: that file's
    CREATE-TABLE/INDEX-IF-NOT-EXISTS pattern can add a new table, but
    can't express 'loosen a constraint on a table that already exists'."""
    columns = conn.execute("PRAGMA table_info(agents)").fetchall()
    owner_col = next((c for c in columns if c[1] == "owner_id"), None)
    if owner_col is None or owner_col[3] == 0:
        # Already nullable (or the table doesn't exist yet, which
        # schema.sql is about to create correctly) -- nothing to do.
        return
    conn.executescript(
        """
        CREATE TABLE agents_new (
            id                  TEXT PRIMARY KEY,
            organization_id     TEXT NOT NULL REFERENCES organizations(id),
            name                TEXT NOT NULL,
            owner_id            TEXT REFERENCES humans(id),
            technical_owner_id  TEXT REFERENCES humans(id),
            environment         TEXT,
            status              TEXT NOT NULL DEFAULT 'unclaimed',
            autonomy_tier       TEXT,
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO agents_new SELECT * FROM agents;
        DROP TABLE agents;
        ALTER TABLE agents_new RENAME TO agents;
        CREATE INDEX idx_agents_owner ON agents(owner_id);
        CREATE INDEX idx_agents_status ON agents(status);
        """
    )
    conn.commit()


def connect(path: str | os.PathLike, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Connect to the database at `path`, creating and initializing it from
    schema.sql only if it doesn't already exist. Safe to call on every
    process start.

    check_same_thread=False is for the console (bf_agent_viewer.console):
    an ASGI app's request handlers aren't guaranteed to run on the thread
    that opened the connection (confirmed the hard way -- Starlette's
    TestClient drives the app through a separate anyio portal thread, and
    a real multi-threaded ASGI server has the same shape of risk). The
    console is read-only and single-connection, so this trades sqlite3's
    same-thread safety net for availability rather than adding real
    concurrent-write risk; the gateway's own connection (concurrent
    writers, hash-chain ordering matters) keeps the default True."""
    path = Path(path)
    is_new = not path.exists()
    conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
    conn.execute("PRAGMA journal_mode=WAL;")
    # NOTE: foreign_keys is deliberately NOT enabled yet. The schema
    # references tools(id) and sessions(id) but nothing populates those
    # tables yet (events log a tool_id/session_id directly). Turning FK
    # enforcement on without first upserting those rows on write would
    # break every event insert. Tracked as a follow-up, not silently
    # claimed as done -- see issues log.
    if is_new:
        with open(_SCHEMA_PATH) as f:
            conn.executescript(f.read())
        conn.commit()
    # migrations.sql is additive-only (CREATE TABLE/INDEX IF NOT EXISTS)
    # and safe to run on every connect(), unlike schema.sql above -- this
    # is how a table added after a database already exists (e.g. F-040's
    # console_credentials/console_sessions) reaches an existing install
    # without a separate migration command to remember to run.
    with open(_MIGRATIONS_PATH) as f:
        conn.executescript(f.read())
    conn.commit()
    _migrate_agents_owner_nullable(conn)
    return conn


def reset(path: str | os.PathLike, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Destructive: wipes and reinitializes the database. For tests and
    local dev only -- never call this from a running gateway process."""
    path = Path(path)
    if path.exists():
        path.unlink()
    return connect(path, check_same_thread=check_same_thread)
