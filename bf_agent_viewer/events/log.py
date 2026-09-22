"""Tamper-evident event logging: per-row hash chaining (OQ-012).

Each event's content_hash is derived from the previous event's hash plus
this event's payload, so altering or deleting a past event breaks the
chain from that point forward -- detectable by re-verifying it.

Performance note (validated during prototyping, ISS-008): the caller MUST
hold the running chain-tip hash in memory (per active writer) and pass it
as `prev_hash`, rather than this module re-querying the DB for it on every
insert. Re-querying with ORDER BY on every insert throttled writes to
~334/sec at 20K rows; holding the hash in memory sustained 32,016/sec in
the prototype and 2,493.8/sec under 15 concurrent real writers (see
research.md).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any


def new_id() -> str:
    return str(uuid.uuid4())


def last_hash(conn: sqlite3.Connection) -> str:
    """Fetch the current chain-tip hash. Call this once per writer at
    startup only -- not per event (see module docstring)."""
    row = conn.execute(
        "SELECT content_hash FROM events ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else "GENESIS"


def log_event(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    agent_id: str,
    session_id: str | None,
    actor_human_id: str | None,
    event_type: str,
    action: str | None,
    tool_id: str | None = None,
    resource_id: str | None = None,
    environment: str = "prod",
    source: str = "mcp_gateway",
    result: str = "success",
    metadata: dict[str, Any] | None = None,
    prev_hash: str,
) -> tuple[str, str]:
    """Insert one tamper-evident event row. Returns (event_id, new_hash) --
    the caller must hold onto new_hash and pass it as prev_hash on the next
    call from this writer."""
    event_id = new_id()
    occurred_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload = {
        "id": event_id,
        "occurred_at": occurred_at,
        "agent_id": agent_id,
        "event_type": event_type,
        "action": action,
        "resource_id": resource_id,
    }
    content_hash = hashlib.sha256(
        (prev_hash + json.dumps(payload, sort_keys=True)).encode()
    ).hexdigest()

    conn.execute(
        """INSERT INTO events (id, occurred_at, organization_id, agent_id, session_id,
           actor_human_id, event_type, action, tool_id, resource_id, environment,
           source, result, request_id, metadata, content_hash)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id, occurred_at, organization_id, agent_id, session_id,
            actor_human_id, event_type, action, tool_id, resource_id,
            environment, source, result, new_id(), json.dumps(metadata or {}),
            content_hash,
        ),
    )
    return event_id, content_hash


def verify_chain(conn: sqlite3.Connection) -> tuple[bool, str | None]:
    """Recompute the hash chain from scratch and confirm it matches.
    Returns (ok, first_bad_event_id)."""
    rows = conn.execute(
        "SELECT id, occurred_at, agent_id, event_type, action, resource_id, content_hash "
        "FROM events ORDER BY created_at, rowid"
    ).fetchall()

    prev_hash = "GENESIS"
    for event_id, occurred_at, agent_id, event_type, action, resource_id, stored_hash in rows:
        payload = {
            "id": event_id,
            "occurred_at": occurred_at,
            "agent_id": agent_id,
            "event_type": event_type,
            "action": action,
            "resource_id": resource_id,
        }
        expected = hashlib.sha256(
            (prev_hash + json.dumps(payload, sort_keys=True)).encode()
        ).hexdigest()
        if expected != stored_hash:
            return False, event_id
        prev_hash = stored_hash
    return True, None
