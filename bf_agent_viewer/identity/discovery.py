"""Passive discovery (F-027, T-012): the other half of dual-path agent
registration, alongside explicit `register_identity`.

Before this, an unrecognized caller (no valid bearer token) had its
traffic logged under a fixed sentinel id, FALLBACK_AGENT_ID, that never
had a real `agents` row behind it -- it existed only as a string stamped
on event rows, invisible to the console's dashboard (which reads FROM
agents), so "who's talking to our gateway that we haven't registered"
had no answer anywhere in the product. That's the actual gap this closes:
the first time an unrecognized caller is seen, it gets a real row --
owner_id NULL, status 'unclaimed' -- so it shows up on the leadership
dashboard (F-046) immediately, not only after someone remembers to run
`register`.

This is explicitly NOT an identity/trust mechanism. The row is keyed by
whatever the connection's self-asserted clientInfo.name happens to be,
and per this project's own stated principle (see gateway/middleware.py's
module docstring), a self-reported client name is never a trust boundary
-- it's a label, nothing more. Two unrelated unregistered callers that
happen to assert the same name will collapse into the same discovered
row, and a caller that asserts nothing at all is genuinely
indistinguishable from any other nameless caller, so those collapse into
one shared row too -- there's no honest way to split them further from
information that was never there. v0.1.0 stays visibility-first: this
makes an unrecognized caller visible, it does not make it trusted.

Claiming (claim_discovered_agent) is the state transition that actually
matters for anything security-relevant: it assigns a real owner AND
issues a real, verifiable identity + credential, which is what F-009
(kill-switch readiness) requires "claimed" to mean -- not just an owner_id
filled in, since an agent that's "owned" but still resolving on nothing
(or on a self-asserted name) isn't actually revocable.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

from .registry import Identity, client_info_name

# Every nameless connection collapses into this one shared, real row --
# there's nothing about it to distinguish it from any other nameless
# connection, so making up separate identities for them would be
# fabricating a distinction the data doesn't support.
UNNAMED_AGENT_ID = "agent-unidentified"
UNNAMED_AGENT_NAME = "Unidentified agent (no client name asserted)"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug or "unnamed"


def discovery_agent_id(client_info: Any) -> str:
    """The agent_id an unregistered caller's traffic is filed under, keyed
    by its self-asserted client name when one is present."""
    name = client_info_name(client_info)
    return f"discovered-{_slug(name)}" if name else UNNAMED_AGENT_ID


def discover_agent(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    client_info: Any,
) -> tuple[str, bool]:
    """Ensure a real, visible-but-unowned `agents` row exists for this
    unregistered caller. Returns (agent_id, first_sighting) -- callers use
    first_sighting to log a one-time discovery event rather than one on
    every single call.

    Safe to call on every request with no extra caching: INSERT OR IGNORE
    against the primary key makes every call after the first a cheap
    no-op, and the `agents` table is already the source of truth every
    other read in this project (console queries, `claim`) goes through --
    a separate in-memory "have I seen this one yet" cache would just be
    a second, driftable copy of what that table already knows.
    """
    agent_id = discovery_agent_id(client_info)
    name = client_info_name(client_info) or UNNAMED_AGENT_NAME
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cur = conn.execute(
        """INSERT OR IGNORE INTO agents
           (id, organization_id, name, owner_id, status, autonomy_tier, created_at, updated_at)
           VALUES (?,?,?,NULL,'unclaimed',NULL,?,?)""",
        (agent_id, organization_id, name, now, now),
    )
    conn.commit()
    return agent_id, cur.rowcount == 1


def claim_discovered_agent(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    agent_id: str,
    owner_human_id: str,
    granted_scope: list[str],
    agent_name: str | None = None,
    autonomy_tier: str = "supervised",
    issuer: str = "bf-agent-viewer-gateway",
) -> Identity:
    """Claim a passively-discovered agent: assigns a real owner and issues
    a real identity + credential (the caller still has to call
    `issue_token` separately, same two-step split `register_identity` /
    `issue_token` already use), taking the row out of the
    visible-but-unowned state `discover_agent` put it in.

    Claiming is a state transition, not just "set owner_id" (ties to
    F-009: kill-switch readiness needs "claimed" to mean
    credential-rotated, not just owned) -- so this refuses to claim an
    agent that isn't currently in the discovered/'unclaimed' state,
    rather than silently re-owning an already-active, explicitly
    registered, or already-claimed agent out from under whoever it
    belongs to.
    """
    row = conn.execute(
        "SELECT status, name FROM agents WHERE id = ? AND organization_id = ?",
        (agent_id, organization_id),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"no discovered agent {agent_id!r} in organization {organization_id!r} -- "
            "check the dashboard or `agents` table for the id a passively-discovered "
            "caller actually landed under"
        )
    status, discovered_name = row
    if status != "unclaimed":
        raise ValueError(
            f"agent {agent_id!r} is not in a discovered/unclaimed state (status={status!r}) "
            "-- it's already claimed or was explicitly registered; use `register` with a "
            "fresh --agent-id for a brand-new identity instead"
        )

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute(
        """UPDATE agents SET owner_id = ?, status = 'active', autonomy_tier = ?, name = ?,
           updated_at = ? WHERE id = ?""",
        (owner_human_id, autonomy_tier, agent_name or discovered_name, now, agent_id),
    )

    # A fresh identity, not a reused/self-asserted one -- post-claim, the
    # bearer token issued next is the trust boundary (same as any
    # explicitly registered identity), not whatever clientInfo.name
    # happened to get this row discovered in the first place.
    identity_id = f"identity-{agent_id}"
    conn.execute(
        """INSERT OR REPLACE INTO agent_identities
           (id, agent_id, identity_type, subject, issuer, valid_from, parent_identity_id,
            granted_scope, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (identity_id, agent_id, "mcp_client_info", agent_id, issuer, now, None,
         json.dumps(list(granted_scope)), now),
    )
    conn.commit()
    return Identity(identity_id, agent_id, None, frozenset(granted_scope))
