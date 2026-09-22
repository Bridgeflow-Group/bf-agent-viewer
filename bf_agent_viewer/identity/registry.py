"""Agent identity registration and resolution.

Real registration API (register_identity) -- the prototype seeded identities
directly into the DB with a throwaway script; this is the first real version
of that path, still simple (no auth/console yet -- that's v0.1.0's console
auth work, F-040) but a real function other code calls, not a one-off.

Identity resolution (load_registry / resolve) is what the gateway calls per
connection to turn a client's asserted clientInfo into a real identity with
a parent link and a scope ceiling. See resolve_client_info_name for a bug
fixed during validation (Sept 22, 2026, OQ-003): clientInfo arrives as a
plain dict from the MCP SDK's modern on_discover path but as an
Implementation object with attribute access from the legacy on_initialize
path -- two shapes for the same field, both handled here explicitly.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Identity:
    identity_id: str
    agent_id: str
    parent_identity_id: str | None
    granted_scope: frozenset[str] | None  # None = unscoped (legacy/unregistered fallback)


def issue_token(conn: sqlite3.Connection, *, agent_id: str, issuer: str = "bf-agent-viewer-gateway") -> str:
    """Issue an opaque bearer token for an agent, stored in the existing
    `credentials` table. This is the real per-request identity mechanism
    (see resolve_by_token) -- found necessary Sept 22, 2026 after testing
    proved MCP's clientInfo (asserted once at discover/initialize) cannot
    be correlated to later tools/call requests in a persistent
    multi-connection gateway process: there is no connection-scoped state
    anywhere in the protocol or in FastMCP's Context for a shared process
    to key a cache on (session_id, request_id, and even Context object
    identity all change on every single request, confirmed empirically,
    not assumed). A token presented on every request sidesteps the
    problem entirely instead of trying to work around it.

    v0.1.0 scope: a simple opaque shared-secret token, not yet the DPoP
    sender-constrained credential planned for the v0.2.0 broker (F-038) --
    that's a real upgrade path, not a redesign, since this table is where
    it lands too.
    """
    token = f"bfav_{secrets.token_urlsafe(32)}"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute(
        """INSERT INTO credentials (id, agent_id, type, issuer, status, issued_at)
           VALUES (?,?,?,?,?,?)""",
        (token, agent_id, "bearer_token", issuer, "active", now),
    )
    conn.commit()
    return token


def register_identity(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    agent_id: str,
    agent_name: str,
    owner_human_id: str,
    subject: str,
    granted_scope: list[str],
    parent_identity_id: str | None = None,
    identity_id: str | None = None,
    issuer: str = "bf-agent-viewer-gateway",
    autonomy_tier: str = "supervised",
) -> Identity:
    """Register a real agent + its identity. `subject` is what a connecting
    client's clientInfo.name must match for the gateway to resolve this
    identity (stands in for a real credential/registration handshake --
    v0.2.0's credential broker replaces this with issued, verifiable
    credentials rather than a self-asserted name).

    Enforces the scope-narrowing invariant at registration time, not just
    hoping callers get it right: a sub-agent's granted_scope must be a
    subset of its parent's.
    """
    if parent_identity_id is not None:
        parent = conn.execute(
            "SELECT granted_scope FROM agent_identities WHERE id = ?", (parent_identity_id,)
        ).fetchone()
        if parent is None:
            raise ValueError(f"parent_identity_id {parent_identity_id!r} does not exist")
        parent_scope = set(json.loads(parent[0])) if parent[0] else None
        if parent_scope is not None and not set(granted_scope) <= parent_scope:
            raise ValueError(
                f"granted_scope {granted_scope} is not a subset of parent's scope "
                f"{sorted(parent_scope)} -- delegation must only narrow authority, never widen it"
            )

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    identity_id = identity_id or f"identity-{agent_id}"

    conn.execute(
        """INSERT OR REPLACE INTO agents
           (id, organization_id, name, owner_id, status, autonomy_tier, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (agent_id, organization_id, agent_name, owner_human_id, "active", autonomy_tier, now, now),
    )
    conn.execute(
        """INSERT OR REPLACE INTO agent_identities
           (id, agent_id, identity_type, subject, issuer, valid_from, parent_identity_id,
            granted_scope, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (identity_id, agent_id, "mcp_client_info", subject, issuer, now,
         parent_identity_id, json.dumps(list(granted_scope)), now),
    )
    conn.commit()
    return Identity(identity_id, agent_id, parent_identity_id, frozenset(granted_scope))


def load_registry(conn: sqlite3.Connection) -> dict[str, Identity]:
    """Load all registered identities, keyed by `subject` (the clientInfo.name
    a connection must assert to resolve to this identity). Kept as a
    secondary, best-effort resolution path -- see module docstring for why
    it's only safe for a single-connection-per-process transport (stdio),
    not the persistent multi-connection HTTP gateway. Call once per
    gateway process at startup, not per request."""
    rows = conn.execute(
        "SELECT subject, id, agent_id, parent_identity_id, granted_scope FROM agent_identities"
    ).fetchall()
    registry: dict[str, Identity] = {}
    for subject, identity_id, agent_id, parent_identity_id, granted_scope in rows:
        scope = frozenset(json.loads(granted_scope)) if granted_scope else None
        registry[subject] = Identity(identity_id, agent_id, parent_identity_id, scope)
    return registry


def load_token_registry(conn: sqlite3.Connection) -> dict[str, Identity]:
    """Load all active bearer tokens, keyed by token value, resolved to the
    issuing agent's identity. This is the primary, transport-safe
    resolution path -- checked fresh per request by the gateway, no
    connection-scoped caching involved. Call once per gateway process at
    startup; a token issued or revoked after that won't be picked up until
    the process restarts or this is called again (acceptable for v0.1.0;
    live invalidation is a v0.2.0+ broker concern)."""
    rows = conn.execute(
        """SELECT c.id, i.id, i.agent_id, i.parent_identity_id, i.granted_scope
           FROM credentials c
           JOIN agent_identities i ON i.agent_id = c.agent_id
           WHERE c.status = 'active'"""
    ).fetchall()
    registry: dict[str, Identity] = {}
    for token, identity_id, agent_id, parent_identity_id, granted_scope in rows:
        scope = frozenset(json.loads(granted_scope)) if granted_scope else None
        registry[token] = Identity(identity_id, agent_id, parent_identity_id, scope)
    return registry


def client_info_name(client_info: Any) -> str | None:
    """Extract .name from an MCP clientInfo value, regardless of which of
    the two shapes it arrives in. Found live (Sept 22, 2026): the SDK's
    modern on_discover path (_meta) gives a plain dict; the legacy
    on_initialize path gives an mcp_types.Implementation object with
    attribute access. getattr() on a dict silently returns None instead of
    erroring, which is exactly how the first version of this check failed
    silently -- every connection fell back to an unscoped default identity
    with no error raised. Branching explicitly on the shape is the fix.
    """
    if client_info is None:
        return None
    if isinstance(client_info, dict):
        return client_info.get("name")
    return getattr(client_info, "name", None)


def resolve(registry: dict[str, Identity], client_info: Any) -> Identity | None:
    """Resolve a connection's asserted clientInfo to a registered identity,
    or None if unregistered (caller decides the fallback -- the gateway
    treats an unresolved identity as unscoped, for backward compatibility
    with clients that never assert a registered name; a stricter posture
    is a v0.2.0+ decision, not this module's to make)."""
    return registry.get(client_info_name(client_info))
