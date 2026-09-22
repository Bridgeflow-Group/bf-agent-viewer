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
    return conn


def reset(path: str | os.PathLike, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Destructive: wipes and reinitializes the database. For tests and
    local dev only -- never call this from a running gateway process."""
    path = Path(path)
    if path.exists():
        path.unlink()
    return connect(path, check_same_thread=check_same_thread)
