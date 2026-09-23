"""Read-only queries backing the console (F-002/003/004/005/008). All of
them are plain SQLite reads against the same schema the gateway writes
to -- the console has no write path of its own in v0.1.0 (visibility
only, matching the whole product's v0.1.0 scope: see docs/security.md).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# F-046: online/offline status indicator, computed at read time from
# last_event_at recency -- not a stored/updated field. Default threshold
# picked as a starting point for v0.1.0, not a settled answer: whether
# this should vary by agent/autonomy tier rather than being one global
# number is still open (see docs/../open-questions.docx OQ-025).
DEFAULT_ONLINE_THRESHOLD_SECONDS = 300.0


def _parse_occurred_at(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def derive_online_status(
    last_event_at: str | None,
    *,
    now: datetime | None = None,
    threshold_seconds: float = DEFAULT_ONLINE_THRESHOLD_SECONDS,
) -> str:
    """Returns "online", "offline", or "never" -- "never" (no activity at
    all) is kept distinct from "offline" (had activity once, but it's
    stale now) since those mean different things to someone reading the
    dashboard: a never-active agent may just not have run yet, not be
    misbehaving."""
    if not last_event_at:
        return "never"
    now = now or datetime.now(timezone.utc)
    elapsed = (now - _parse_occurred_at(last_event_at)).total_seconds()
    return "online" if elapsed < threshold_seconds else "offline"


@dataclass(frozen=True)
class AgentRow:
    id: str
    name: str
    owner_name: str | None
    status: str
    environment: str | None
    autonomy_tier: str | None
    last_event_at: str | None
    event_count: int
    online_status: str


@dataclass(frozen=True)
class EventRow:
    id: str
    occurred_at: str
    agent_id: str
    agent_name: str | None
    event_type: str
    action: str | None
    tool_id: str | None
    result: str | None
    source: str | None


@dataclass(frozen=True)
class EventDetail(EventRow):
    organization_id: str
    session_id: str | None
    actor_human_id: str | None
    resource_id: str | None
    environment: str | None
    request_id: str | None
    metadata: dict[str, Any]
    content_hash: str


@dataclass(frozen=True)
class AgentDetail(AgentRow):
    organization_id: str
    owner_id: str
    technical_owner_id: str | None
    parent_agent_id: str | None
    parent_agent_name: str | None
    granted_scope: list[str] | None


def has_any_agents(conn: sqlite3.Connection) -> bool:
    """Onboarding (F-001): the dashboard's empty state depends on
    whether anything has been registered yet at all."""
    row = conn.execute("SELECT 1 FROM agents LIMIT 1").fetchone()
    return row is not None


def list_agents(
    conn: sqlite3.Connection,
    *,
    organization_id: str | None = None,
    online_threshold_seconds: float = DEFAULT_ONLINE_THRESHOLD_SECONDS,
) -> list[AgentRow]:
    where = "WHERE a.organization_id = ?" if organization_id else ""
    params = (organization_id,) if organization_id else ()
    rows = conn.execute(
        f"""
        SELECT a.id, a.name, h.name, a.status, a.environment, a.autonomy_tier,
               MAX(e.occurred_at) AS last_event_at, COUNT(e.id) AS event_count
        FROM agents a
        LEFT JOIN humans h ON h.id = a.owner_id
        LEFT JOIN events e ON e.agent_id = a.id
        {where}
        GROUP BY a.id
        ORDER BY last_event_at IS NULL, last_event_at DESC, a.name
        """,
        params,
    ).fetchall()
    now = datetime.now(timezone.utc)
    return [
        AgentRow(*row, online_status=derive_online_status(row[6], now=now, threshold_seconds=online_threshold_seconds))
        for row in rows
    ]


def get_agent(
    conn: sqlite3.Connection,
    agent_id: str,
    *,
    online_threshold_seconds: float = DEFAULT_ONLINE_THRESHOLD_SECONDS,
) -> AgentDetail | None:
    row = conn.execute(
        """
        SELECT a.id, a.name, h.name, a.status, a.environment, a.autonomy_tier,
               a.organization_id, a.owner_id, a.technical_owner_id
        FROM agents a
        LEFT JOIN humans h ON h.id = a.owner_id
        WHERE a.id = ?
        """,
        (agent_id,),
    ).fetchone()
    if row is None:
        return None
    (aid, name, owner_name, status, environment, autonomy_tier,
     organization_id, owner_id, technical_owner_id) = row

    counts = conn.execute(
        "SELECT MAX(occurred_at), COUNT(*) FROM events WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    last_event_at, event_count = counts if counts else (None, 0)

    identity = conn.execute(
        """
        SELECT ai.parent_identity_id, ai.granted_scope
        FROM agent_identities ai
        WHERE ai.agent_id = ?
        ORDER BY ai.created_at DESC LIMIT 1
        """,
        (agent_id,),
    ).fetchone()
    parent_agent_id = None
    parent_agent_name = None
    granted_scope = None
    if identity:
        parent_identity_id, granted_scope_json = identity
        if granted_scope_json:
            granted_scope = sorted(json.loads(granted_scope_json))
        if parent_identity_id:
            parent_row = conn.execute(
                """
                SELECT ag.id, ag.name FROM agent_identities pi
                JOIN agents ag ON ag.id = pi.agent_id
                WHERE pi.id = ?
                """,
                (parent_identity_id,),
            ).fetchone()
            if parent_row:
                parent_agent_id, parent_agent_name = parent_row

    return AgentDetail(
        id=aid, name=name, owner_name=owner_name, status=status, environment=environment,
        autonomy_tier=autonomy_tier, last_event_at=last_event_at, event_count=event_count,
        online_status=derive_online_status(last_event_at, threshold_seconds=online_threshold_seconds),
        organization_id=organization_id, owner_id=owner_id, technical_owner_id=technical_owner_id,
        parent_agent_id=parent_agent_id, parent_agent_name=parent_agent_name,
        granted_scope=granted_scope,
    )


def list_events(
    conn: sqlite3.Connection, *, agent_id: str | None = None, limit: int = 50,
) -> list[EventRow]:
    where = "WHERE e.agent_id = ?" if agent_id else ""
    params: tuple = (agent_id, limit) if agent_id else (limit,)
    rows = conn.execute(
        f"""
        SELECT e.id, e.occurred_at, e.agent_id, a.name, e.event_type, e.action,
               e.tool_id, e.result, e.source
        FROM events e
        LEFT JOIN agents a ON a.id = e.agent_id
        {where}
        ORDER BY e.occurred_at DESC, e.rowid DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [EventRow(*row) for row in rows]


def get_event(conn: sqlite3.Connection, event_id: str) -> EventDetail | None:
    row = conn.execute(
        """
        SELECT e.id, e.occurred_at, e.agent_id, a.name, e.event_type, e.action,
               e.tool_id, e.result, e.source, e.organization_id, e.session_id,
               e.actor_human_id, e.resource_id, e.environment, e.request_id,
               e.metadata, e.content_hash
        FROM events e
        LEFT JOIN agents a ON a.id = e.agent_id
        WHERE e.id = ?
        """,
        (event_id,),
    ).fetchone()
    if row is None:
        return None
    (eid, occurred_at, agent_id, agent_name, event_type, action, tool_id, result, source,
     organization_id, session_id, actor_human_id, resource_id, environment, request_id,
     metadata_json, content_hash) = row
    return EventDetail(
        id=eid, occurred_at=occurred_at, agent_id=agent_id, agent_name=agent_name,
        event_type=event_type, action=action, tool_id=tool_id, result=result, source=source,
        organization_id=organization_id, session_id=session_id, actor_human_id=actor_human_id,
        resource_id=resource_id, environment=environment, request_id=request_id,
        metadata=json.loads(metadata_json) if metadata_json else {}, content_hash=content_hash,
    )


def search(conn: sqlite3.Connection, query: str, *, limit: int = 25) -> dict[str, list]:
    """F-005: a single search box over both agents and events. Deliberately
    simple (SQL LIKE, not a search index) -- matches the SMB/low-infra
    positioning (see docs/positioning.md): no extra search service to run
    for what's realistically a few thousand rows at this scale."""
    like = f"%{query}%"
    agent_rows = conn.execute(
        """
        SELECT a.id, a.name, h.name, a.status, a.environment, a.autonomy_tier, NULL, 0
        FROM agents a
        LEFT JOIN humans h ON h.id = a.owner_id
        WHERE a.name LIKE ? OR a.id LIKE ?
        ORDER BY a.name LIMIT ?
        """,
        (like, like, limit),
    ).fetchall()
    event_rows = conn.execute(
        """
        SELECT e.id, e.occurred_at, e.agent_id, a.name, e.event_type, e.action, e.tool_id,
               e.result, e.source
        FROM events e
        LEFT JOIN agents a ON a.id = e.agent_id
        WHERE e.id LIKE ? OR e.event_type LIKE ? OR e.action LIKE ? OR e.tool_id LIKE ?
           OR a.name LIKE ?
        ORDER BY e.occurred_at DESC LIMIT ?
        """,
        (like, like, like, like, like, limit),
    ).fetchall()
    return {
        "agents": [AgentRow(*row, online_status="never") for row in agent_rows],
        "events": [EventRow(*row) for row in event_rows],
    }
