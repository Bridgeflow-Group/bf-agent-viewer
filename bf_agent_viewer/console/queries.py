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
class DelegationNode:
    """One node in a delegation tree (F-028/T-015): a sub-agent nested
    under its parent's agent detail view, with its own sub-agents nested
    the same way underneath it -- full multi-hop delegation, not just the
    immediate parent/child link. `children` is empty for a leaf."""
    id: str
    name: str
    status: str
    autonomy_tier: str | None
    online_status: str
    children: list["DelegationNode"]


@dataclass(frozen=True)
class AgentDetail(AgentRow):
    organization_id: str
    owner_id: str
    technical_owner_id: str | None
    parent_agent_id: str | None
    parent_agent_name: str | None
    granted_scope: list[str] | None
    # F-028/T-015: root -> immediate-parent order, the full chain above
    # this agent, not just the one-hop parent_agent_id/name above (kept
    # for backward compatibility -- it's just ancestor_chain[-1]).
    ancestor_chain: list[tuple[str, str]]
    # F-028/T-015: the full nested delegation tree of everything this
    # agent has spawned, direct children and their own descendants alike
    # -- what actually lets agent_detail.html show "the full delegation
    # tree", not just a one-hop parent link.
    sub_agents: list[DelegationNode]


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
    include_sub_agents: bool = False,
) -> list[AgentRow]:
    """F-028/T-015 (per OQ-007's decision): a sub-agent spawn still gets a
    full, permanent Agent Identity (accountability and the future
    credential-revocation kill switch both need one per delegation hop --
    see F-026), but by default it does NOT show up here as a peer row
    alongside top-level agents. It nests instead under its parent's own
    agent detail view (see get_agent's sub_agents field, below) -- a
    one-shot orchestrator spawning ten sub-agents shouldn't flood this
    flat list with ten more rows. `include_sub_agents=True` opts back
    into the old flat listing (e.g. for a future "show everything" view
    or a script that genuinely wants every agent row); OQ-007 leaves open
    exactly when a recurring sub-agent role should get "promoted" to its
    own first-class top-level row -- that promotion heuristic isn't built
    yet, so for now every sub-agent stays nested, full stop.
    """
    where_parts = []
    params: list = []
    if organization_id:
        where_parts.append("a.organization_id = ?")
        params.append(organization_id)
    if not include_sub_agents:
        where_parts.append(
            """NOT EXISTS (
                SELECT 1 FROM agent_identities ai
                WHERE ai.agent_id = a.id AND ai.parent_identity_id IS NOT NULL
            )"""
        )
    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
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
        tuple(params),
    ).fetchall()
    now = datetime.now(timezone.utc)
    return [
        AgentRow(*row, online_status=derive_online_status(row[6], now=now, threshold_seconds=online_threshold_seconds))
        for row in rows
    ]


def _ancestor_chain(
    conn: sqlite3.Connection,
    parent_identity_id: str | None,
    *,
    organization_id: str | None = None,
    max_hops: int = 20,
) -> list[tuple[str, str]]:
    """F-028/T-015: walk parent_identity_id all the way to the root,
    returning [(agent_id, agent_name), ...] in root -> immediate-parent
    order (breadcrumb order) -- agent_detail.html's old "Delegated from"
    row only ever showed the one-hop parent, requiring a click per hop to
    see anything further up. max_hops is a defensive cap against a
    malformed cycle (delegation should never produce one, given the
    scope-narrowing invariant, but this is a read path -- it should never
    hang even if the data is somehow wrong)."""
    chain: list[tuple[str, str]] = []
    seen_agent_ids: set[str] = set()
    current = parent_identity_id
    while current is not None and len(chain) < max_hops:
        where = "WHERE ai.id = ?"
        params: tuple = (current,)
        if organization_id is not None:
            where += " AND ag.organization_id = ?"
            params += (organization_id,)
        row = conn.execute(
            f"""
            SELECT ai.parent_identity_id, ag.id, ag.name
            FROM agent_identities ai
            JOIN agents ag ON ag.id = ai.agent_id
            {where}
            """,
            params,
        ).fetchone()
        if row is None:
            break
        next_parent_identity_id, agent_id, agent_name = row
        if agent_id in seen_agent_ids:
            break  # cycle guard -- should never happen, never hang if it does
        seen_agent_ids.add(agent_id)
        chain.append((agent_id, agent_name))
        current = next_parent_identity_id
    chain.reverse()
    return chain


def _delegation_children(
    conn: sqlite3.Connection,
    agent_id: str,
    *,
    organization_id: str | None = None,
    online_threshold_seconds: float = DEFAULT_ONLINE_THRESHOLD_SECONDS,
    now: datetime | None = None,
    _seen_agent_ids: frozenset[str] = frozenset(),
    _max_depth: int = 20,
) -> list[DelegationNode]:
    """F-028/T-015: the direct sub-agents this agent has spawned, each
    with its own sub_agents nested the same way -- the full tree below
    this agent, not just one hop. `_seen_agent_ids`/`_max_depth` are a
    cycle guard (delegation's scope-narrowing invariant should make a
    cycle impossible, but this recurses over live data on every page
    load, so it should never be able to hang or infinite-loop even if
    that's ever violated)."""
    if agent_id in _seen_agent_ids or _max_depth <= 0:
        return []
    now = now or datetime.now(timezone.utc)
    where = "WHERE parent_ai.agent_id = ?"
    params: list = [agent_id]
    if organization_id is not None:
        where += " AND child_agent.organization_id = ?"
        params.append(organization_id)
    rows = conn.execute(
        f"""
        SELECT child_agent.id, child_agent.name, child_agent.status, child_agent.autonomy_tier,
               MAX(e.occurred_at) AS last_event_at
        FROM agent_identities parent_ai
        JOIN agent_identities child_ai ON child_ai.parent_identity_id = parent_ai.id
        JOIN agents child_agent ON child_agent.id = child_ai.agent_id
        LEFT JOIN events e ON e.agent_id = child_agent.id
        {where}
        GROUP BY child_agent.id
        ORDER BY child_agent.name
        """,
        tuple(params),
    ).fetchall()
    seen_next = _seen_agent_ids | {agent_id}
    return [
        DelegationNode(
            id=child_id, name=child_name, status=child_status, autonomy_tier=child_autonomy_tier,
            online_status=derive_online_status(last_event_at, now=now, threshold_seconds=online_threshold_seconds),
            children=_delegation_children(
                conn, child_id, organization_id=organization_id,
                online_threshold_seconds=online_threshold_seconds, now=now,
                _seen_agent_ids=seen_next, _max_depth=_max_depth - 1,
            ),
        )
        for child_id, child_name, child_status, child_autonomy_tier, last_event_at in rows
    ]


def get_agent(
    conn: sqlite3.Connection,
    agent_id: str,
    *,
    organization_id: str | None = None,
    online_threshold_seconds: float = DEFAULT_ONLINE_THRESHOLD_SECONDS,
) -> AgentDetail | None:
    # T-014 (org isolation audit): organization_id, when given, is a real
    # filter, not just decoration -- without it, a console started with
    # `--org acme` still returned full agent detail for direct navigation
    # to any other organization's agent id, even though the dashboard's
    # own listing (list_agents, above) already respected --org.
    where = "WHERE a.id = ?"
    params: tuple = (agent_id,)
    if organization_id is not None:
        where += " AND a.organization_id = ?"
        params += (organization_id,)
    row = conn.execute(
        f"""
        SELECT a.id, a.name, h.name, a.status, a.environment, a.autonomy_tier,
               a.organization_id, a.owner_id, a.technical_owner_id
        FROM agents a
        LEFT JOIN humans h ON h.id = a.owner_id
        {where}
        """,
        params,
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

    # F-028/T-015: the full delegation tree, both directions -- everything
    # above this agent (ancestor_chain) and everything below it
    # (sub_agents), not just the one-hop parent link computed above.
    ancestor_chain = _ancestor_chain(
        conn, identity[0] if identity else None, organization_id=organization_id,
    )
    sub_agents = _delegation_children(
        conn, agent_id, organization_id=organization_id,
        online_threshold_seconds=online_threshold_seconds,
    )

    return AgentDetail(
        id=aid, name=name, owner_name=owner_name, status=status, environment=environment,
        autonomy_tier=autonomy_tier, last_event_at=last_event_at, event_count=event_count,
        online_status=derive_online_status(last_event_at, threshold_seconds=online_threshold_seconds),
        organization_id=organization_id, owner_id=owner_id, technical_owner_id=technical_owner_id,
        parent_agent_id=parent_agent_id, parent_agent_name=parent_agent_name,
        granted_scope=granted_scope, ancestor_chain=ancestor_chain, sub_agents=sub_agents,
    )


def list_events(
    conn: sqlite3.Connection,
    *,
    agent_id: str | None = None,
    organization_id: str | None = None,
    limit: int = 50,
) -> list[EventRow]:
    # T-014 (org isolation audit): organization_id, when given, is a real
    # filter -- previously this had no organization awareness at all, so
    # the dashboard's "recent activity" panel (called with no agent_id)
    # showed events from every organization in the database even when the
    # agent list right above it was correctly restricted via --org, and
    # an agent detail page could show another organization's events for
    # an agent_id that happened to belong to it (events.agent_id isn't
    # itself validated against the requested organization elsewhere).
    conditions = []
    params: list = []
    if agent_id:
        conditions.append("e.agent_id = ?")
        params.append(agent_id)
    if organization_id is not None:
        conditions.append("e.organization_id = ?")
        params.append(organization_id)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
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
        tuple(params),
    ).fetchall()
    return [EventRow(*row) for row in rows]


def get_event(
    conn: sqlite3.Connection, event_id: str, *, organization_id: str | None = None,
) -> EventDetail | None:
    # T-014 (org isolation audit): organization_id, when given, is a real
    # filter -- previously a console restricted to one org via --org still
    # showed full event detail (including its metadata payload) for any
    # event_id in the database, regardless of organization, via direct
    # navigation to /events/<id>.
    where = "WHERE e.id = ?"
    params: tuple = (event_id,)
    if organization_id is not None:
        where += " AND e.organization_id = ?"
        params += (organization_id,)
    row = conn.execute(
        f"""
        SELECT e.id, e.occurred_at, e.agent_id, a.name, e.event_type, e.action,
               e.tool_id, e.result, e.source, e.organization_id, e.session_id,
               e.actor_human_id, e.resource_id, e.environment, e.request_id,
               e.metadata, e.content_hash
        FROM events e
        LEFT JOIN agents a ON a.id = e.agent_id
        {where}
        """,
        params,
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


def search(
    conn: sqlite3.Connection, query: str, *, organization_id: str | None = None, limit: int = 25,
) -> dict[str, list]:
    """F-005: a single search box over both agents and events. Deliberately
    simple (SQL LIKE, not a search index) -- matches the SMB/low-infra
    positioning (see docs/positioning.md): no extra search service to run
    for what's realistically a few thousand rows at this scale.

    organization_id (T-014, org isolation audit): when given, is a real
    filter -- previously this searched across every organization in the
    database unconditionally, so the search box was a complete bypass of
    a console instance's --org restriction (the one query function with
    zero awareness that organizations existed at all).
    """
    like = f"%{query}%"
    agent_where = "WHERE (a.name LIKE ? OR a.id LIKE ?)"
    agent_params: list = [like, like]
    event_where = "WHERE (e.id LIKE ? OR e.event_type LIKE ? OR e.action LIKE ? OR e.tool_id LIKE ? OR a.name LIKE ?)"
    event_params: list = [like, like, like, like, like]
    if organization_id is not None:
        agent_where += " AND a.organization_id = ?"
        agent_params.append(organization_id)
        event_where += " AND e.organization_id = ?"
        event_params.append(organization_id)
    agent_params.append(limit)
    event_params.append(limit)

    agent_rows = conn.execute(
        f"""
        SELECT a.id, a.name, h.name, a.status, a.environment, a.autonomy_tier, NULL, 0
        FROM agents a
        LEFT JOIN humans h ON h.id = a.owner_id
        {agent_where}
        ORDER BY a.name LIMIT ?
        """,
        tuple(agent_params),
    ).fetchall()
    event_rows = conn.execute(
        f"""
        SELECT e.id, e.occurred_at, e.agent_id, a.name, e.event_type, e.action, e.tool_id,
               e.result, e.source
        FROM events e
        LEFT JOIN agents a ON a.id = e.agent_id
        {event_where}
        ORDER BY e.occurred_at DESC LIMIT ?
        """,
        tuple(event_params),
    ).fetchall()
    return {
        "agents": [AgentRow(*row, online_status="never") for row in agent_rows],
        "events": [EventRow(*row) for row in event_rows],
    }
