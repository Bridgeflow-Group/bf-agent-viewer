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

Retention (F-019/T-017): `bf_agent_viewer.retention.prune_events` deletes
old rows from `events` once they're past the configured retention window.
That's a real complication for a hash chain that assumes it starts at the
literal string "GENESIS" -- once the true first row is gone, replaying
from GENESIS against whatever's left no longer matches. `last_hash()` and
`verify_chain()` below both fall back to the latest row in
`retention_checkpoints` (the content_hash of the last row a prune run
removed) instead of GENESIS, so both writing new events and verifying the
chain keep working correctly across a pruned history. See
bf_agent_viewer/retention/prune.py for how that checkpoint gets written.
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


def _latest_checkpoint_hash(conn: sqlite3.Connection) -> str | None:
    """The chain_tip_hash of the most recent retention prune (F-019), or
    None if no prune has ever run against this database. Tolerates the
    table not existing yet (a connection opened without going through
    bf_agent_viewer.db.connect()'s migrations, e.g. some hand-rolled test
    setup) by treating that the same as "no checkpoint"."""
    try:
        row = conn.execute(
            "SELECT chain_tip_hash FROM retention_checkpoints ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def last_hash(conn: sqlite3.Connection) -> str:
    """Fetch the current chain-tip hash. Call this once per writer at
    startup only -- not per event (see module docstring).

    Falls back to the latest retention checkpoint, then to "GENESIS", when
    `events` itself has no rows -- which happens not only on a genuinely
    empty database, but also when a retention prune has removed every
    event logged so far (an aggressively short --retention-days). Without
    this fallback the next event written after a full prune would chain
    from GENESIS while `verify_chain()` (below) correctly expects it to
    chain from the checkpoint -- a false tamper report on the very next
    write, not a real one."""
    row = conn.execute(
        "SELECT content_hash FROM events ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if row:
        return row[0]
    return _latest_checkpoint_hash(conn) or "GENESIS"


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
    Returns (ok, first_bad_event_id).

    Starts the replay from the latest retention checkpoint's
    chain_tip_hash rather than assuming an unpruned history back to
    GENESIS -- correct whether or not F-019 retention pruning has ever
    run against this database (see module docstring and
    bf_agent_viewer/retention/prune.py)."""
    rows = conn.execute(
        "SELECT id, occurred_at, agent_id, event_type, action, resource_id, content_hash "
        "FROM events ORDER BY created_at, rowid"
    ).fetchall()

    prev_hash = _latest_checkpoint_hash(conn) or "GENESIS"
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
