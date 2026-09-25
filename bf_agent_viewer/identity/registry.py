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


def _now_str(offset_seconds: float = 0.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_seconds))


def issue_token(
    conn: sqlite3.Connection, *, agent_id: str, issuer: str = "bf-agent-viewer-gateway",
    ttl_seconds: float | None = None,
) -> str:
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

    ttl_seconds (F-016/T-024, v0.2.0): optional. None (the default) issues
    a standing token with no expiry -- unchanged v0.1.0 behavior, so
    nothing that already calls this without the new keyword changes
    behavior. Passing ttl_seconds sets `expiry` to now + that many
    seconds; load_token_registry() stops resolving it once that's passed,
    and renew_token() is the only way to push it back out. This is the
    mechanism basis for F-009's kill switch (see OQ-004): a credential
    that's short-lived and simply isn't renewed lapses on its own within
    one refresh interval, without needing a separate real-time revocation
    path -- revoke_token() exists alongside it for an immediate cutoff
    rather than waiting out the TTL.
    """
    token = f"bfav_{secrets.token_urlsafe(32)}"
    now = _now_str()
    expiry = _now_str(ttl_seconds) if ttl_seconds is not None else None
    conn.execute(
        """INSERT INTO credentials (id, agent_id, type, issuer, status, issued_at, expiry)
           VALUES (?,?,?,?,?,?,?)""",
        (token, agent_id, "bearer_token", issuer, "active", now, expiry),
    )
    conn.commit()
    return token


def renew_token(conn: sqlite3.Connection, token: str, *, ttl_seconds: float) -> str:
    """Push a short-lived credential's expiry back out by ttl_seconds from
    now. The other half of F-009's kill-switch mechanism (see OQ-004):
    an agent (or whatever schedules renewal on its behalf, e.g. an
    operator's cron -- v0.1.0/early v0.2.0 doesn't yet have the agent
    call this itself) keeps calling this on an interval shorter than the
    TTL; the kill switch is simply *not* doing that anymore, either by
    stopping the schedule or by calling revoke_token() for an immediate
    cutoff instead of waiting for the TTL to lapse.

    Deliberately refuses to renew a credential that isn't active or has
    already expired -- renewal must not be able to resurrect a credential
    that's already been cut off; issue a new one instead. Raises
    ValueError in either case, and if the token doesn't exist at all.
    """
    row = conn.execute(
        "SELECT status, expiry FROM credentials WHERE id = ?", (token,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no credential {token!r}")
    status, expiry = row
    if status != "active":
        raise ValueError(f"credential {token!r} is not active (status={status!r}) -- cannot renew it")
    now = _now_str()
    if expiry is not None and expiry <= now:
        raise ValueError(f"credential {token!r} already expired at {expiry} -- issue a new one instead of renewing")
    new_expiry = _now_str(ttl_seconds)
    conn.execute("UPDATE credentials SET expiry = ? WHERE id = ?", (new_expiry, token))
    conn.commit()
    return new_expiry


def revoke_token(conn: sqlite3.Connection, token: str) -> None:
    """Immediately cut off a credential -- sets status='revoked' and
    records revoked_at, rather than waiting for a TTL to lapse. Works on
    any active credential, short-lived or standing (a v0.1.0 token issued
    with no expiry can still be revoked this way; TTL and revocation are
    independent mechanisms, not one built only for the other). Excluded
    from load_token_registry() the next time it refreshes -- see
    GatewayMiddleware's credential_refresh_seconds for how soon that is.
    Raises ValueError if there's no active credential by this id to revoke
    (already revoked, or never existed) -- revoking is not idempotent
    against a caller's mistake, it should surface one.
    """
    now = _now_str()
    cursor = conn.execute(
        "UPDATE credentials SET status = 'revoked', revoked_at = ? WHERE id = ? AND status = 'active'",
        (now, token),
    )
    conn.commit()
    if cursor.rowcount == 0:
        raise ValueError(f"no active credential {token!r} to revoke")


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
        # T-014 (org isolation audit): join through to agents.organization_id
        # rather than a bare id lookup -- a parent_identity_id is not itself
        # organization-scoped, so without this a sub-agent registration
        # under one organization could inherit (and be scope-checked
        # against) a parent identity that belongs to a *different*
        # organization, as long as the caller could guess/enumerate its id.
        parent = conn.execute(
            """SELECT ai.granted_scope FROM agent_identities ai
               JOIN agents ag ON ag.id = ai.agent_id
               WHERE ai.id = ? AND ag.organization_id = ?""",
            (parent_identity_id, organization_id),
        ).fetchone()
        if parent is None:
            raise ValueError(
                f"parent_identity_id {parent_identity_id!r} does not exist in "
                f"organization {organization_id!r}"
            )
        parent_scope = set(json.loads(parent[0])) if parent[0] else None
        if parent_scope is not None and not set(granted_scope) <= parent_scope:
            raise ValueError(
                f"granted_scope {granted_scope} is not a subset of parent's scope "
                f"{sorted(parent_scope)} -- delegation must only narrow authority, never widen it"
            )

    # T-014 (org isolation audit): owner_human_id must belong to the same
    # organization as the agent being registered. Without this, an agent
    # created under organization A could be assigned an owner_id
    # referencing a human row that actually belongs to organization B --
    # accountability (the whole point of owner_id -- see security.md's
    # "every event traces back through that chain to an accountable human
    # owner") would then point at the wrong organization's records.
    owner_row = conn.execute(
        "SELECT 1 FROM humans WHERE id = ? AND organization_id = ?",
        (owner_human_id, organization_id),
    ).fetchone()
    if owner_row is None:
        raise ValueError(
            f"owner_human_id {owner_human_id!r} does not exist in organization "
            f"{organization_id!r} -- create the human there first with `bf-agent-viewer human create`"
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


def load_registry(
    conn: sqlite3.Connection, *, organization_id: str | None = None,
) -> dict[str, Identity]:
    """Load all registered identities, keyed by `subject` (the clientInfo.name
    a connection must assert to resolve to this identity). Kept as a
    secondary, best-effort resolution path -- see module docstring for why
    it's only safe for a single-connection-per-process transport (stdio),
    not the persistent multi-connection HTTP gateway. Call once per
    gateway process at startup, not per request.

    organization_id (T-014, org isolation audit): filters to identities
    whose agent belongs to this organization. Without it, two
    organizations sharing one database file and happening to register the
    same `subject` string would collide -- the second load would silently
    win the dict key, and a client asserting that name could resolve to
    the wrong organization's identity. Optional for backward
    compatibility (some tests build a registry from a single-org fixture
    and don't care); every real caller in this codebase now passes it.
    """
    if organization_id is not None:
        rows = conn.execute(
            """SELECT ai.subject, ai.id, ai.agent_id, ai.parent_identity_id, ai.granted_scope
               FROM agent_identities ai
               JOIN agents ag ON ag.id = ai.agent_id
               WHERE ag.organization_id = ?""",
            (organization_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT subject, id, agent_id, parent_identity_id, granted_scope FROM agent_identities"
        ).fetchall()
    registry: dict[str, Identity] = {}
    for subject, identity_id, agent_id, parent_identity_id, granted_scope in rows:
        scope = frozenset(json.loads(granted_scope)) if granted_scope else None
        registry[subject] = Identity(identity_id, agent_id, parent_identity_id, scope)
    return registry


def load_token_registry(
    conn: sqlite3.Connection, *, organization_id: str | None = None,
) -> dict[str, Identity]:
    """Load all active, unexpired bearer tokens, keyed by token value,
    resolved to the issuing agent's identity. This is the primary,
    transport-safe resolution path -- checked fresh per request by the
    gateway, no connection-scoped caching involved. Call once per gateway
    process at startup; a token issued, renewed, or revoked after that
    won't be picked up until this is called again -- see
    GatewayMiddleware.credential_refresh_seconds (F-016/T-024) for how the
    gateway itself now calls this again periodically rather than only
    once, which is what makes a short-lived credential's expiry (or an
    explicit revoke_token() call) actually take effect without a process
    restart.

    Expiry filter (F-016/T-024): a credential with a non-null `expiry` in
    the past is excluded here, the same as a revoked one -- both are
    "not usable right now," and this is the one place both v0.1.0's
    standing tokens (expiry always NULL, unaffected) and v0.2.0's
    short-lived ones are checked, so nothing downstream needs its own
    separate expiry logic.

    organization_id (T-014, org isolation audit): the most severe gap this
    audit found. This is the gateway's actual authentication boundary --
    without this filter, a bearer token issued for agent X in
    organization A would resolve successfully against a gateway process
    configured for organization B, as long as both organizations' data
    live in the same SQLite file. A gateway is always started with one
    `--org`/BF_ORG (see cli.py), so this is now filtered to match it:
    a token from another organization simply isn't in the loaded registry
    at all, not merely displayed differently. Optional for backward
    compatibility (a caller resolving across all orgs deliberately, if
    one ever legitimately needs to); every real caller in this codebase
    now passes it.
    """
    now = _now_str()
    if organization_id is not None:
        rows = conn.execute(
            """SELECT c.id, i.id, i.agent_id, i.parent_identity_id, i.granted_scope
               FROM credentials c
               JOIN agent_identities i ON i.agent_id = c.agent_id
               JOIN agents ag ON ag.id = i.agent_id
               WHERE c.status = 'active' AND (c.expiry IS NULL OR c.expiry > ?)
                 AND ag.organization_id = ?""",
            (now, organization_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT c.id, i.id, i.agent_id, i.parent_identity_id, i.granted_scope
               FROM credentials c
               JOIN agent_identities i ON i.agent_id = c.agent_id
               WHERE c.status = 'active' AND (c.expiry IS NULL OR c.expiry > ?)""",
            (now,),
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
