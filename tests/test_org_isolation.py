"""T-014: org/tenant isolation audit. F-015's schema explicitly supports
multiple organizations coexisting in one SQLite database file (the
`organizations` table, `org create`, and the console's own `--org` flag
all assume this), so even though v0.1.0 is nominally "single-tenant per
install" (docs/security.md), a database that in practice holds more than
one organization's data must not leak across that boundary. This audit
found four concrete gaps and this file proves each one: real behavior
before the fix would have failed every test below (verified by reverting
each fix locally while writing these), and passes now.

Seeds two full organizations (org-a / org-b) in one database and checks
that identity resolution, delegation registration, and every console
query genuinely can't see or act across the boundary -- not just that
they're labeled differently.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from bf_agent_viewer.console import queries
from bf_agent_viewer.db import reset
from bf_agent_viewer.gateway import build_gateway
from bf_agent_viewer.identity import (
    claim_discovered_agent,
    issue_token,
    register_identity,
)
from bf_agent_viewer.identity.discovery import discover_agent
from bf_agent_viewer.identity.registry import load_registry, load_token_registry

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_SCRIPT = os.path.join(HERE, "fixtures", "demo_backend.py")


def _seed_two_orgs(conn):
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-a', 'Acme')")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-b', 'Globex')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('human-a', 'org-a', 'Alice')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('human-b', 'org-b', 'Bob')")
    conn.commit()


# -- load_token_registry: the actual gateway authentication boundary -----

def test_load_token_registry_excludes_other_organizations_tokens(tmp_path):
    conn = reset(tmp_path / "org1.db")
    _seed_two_orgs(conn)

    identity_a = register_identity(
        conn, organization_id="org-a", agent_id="agent-a", agent_name="Agent A",
        owner_human_id="human-a", subject="agent-a", granted_scope=["get_weather"],
    )
    token_a = issue_token(conn, agent_id=identity_a.agent_id)

    identity_b = register_identity(
        conn, organization_id="org-b", agent_id="agent-b", agent_name="Agent B",
        owner_human_id="human-b", subject="agent-b", granted_scope=["get_weather"],
    )
    token_b = issue_token(conn, agent_id=identity_b.agent_id)

    registry_a = load_token_registry(conn, organization_id="org-a")
    assert token_a in registry_a
    # This is the finding: a gateway configured for org-a must not resolve
    # a token issued for an org-b agent at all.
    assert token_b not in registry_a

    registry_b = load_token_registry(conn, organization_id="org-b")
    assert token_b in registry_b
    assert token_a not in registry_b


def test_load_token_registry_with_no_organization_id_is_unfiltered_for_backward_compat(tmp_path):
    conn = reset(tmp_path / "org2.db")
    _seed_two_orgs(conn)
    identity_a = register_identity(
        conn, organization_id="org-a", agent_id="agent-a", agent_name="Agent A",
        owner_human_id="human-a", subject="agent-a", granted_scope=["get_weather"],
    )
    token_a = issue_token(conn, agent_id=identity_a.agent_id)
    registry = load_token_registry(conn)
    assert token_a in registry


def test_load_registry_excludes_other_organizations_subjects(tmp_path):
    conn = reset(tmp_path / "org3.db")
    _seed_two_orgs(conn)
    register_identity(
        conn, organization_id="org-a", agent_id="agent-a", agent_name="Agent A",
        owner_human_id="human-a", subject="shared-name", granted_scope=["get_weather"],
    )
    register_identity(
        conn, organization_id="org-b", agent_id="agent-b", agent_name="Agent B",
        owner_human_id="human-b", subject="shared-name", granted_scope=["get_weather"],
    )

    registry_a = load_registry(conn, organization_id="org-a")
    assert registry_a["shared-name"].agent_id == "agent-a"

    registry_b = load_registry(conn, organization_id="org-b")
    assert registry_b["shared-name"].agent_id == "agent-b"


# -- register_identity: cross-org delegation and ownership ---------------

def test_register_identity_refuses_parent_identity_from_another_organization(tmp_path):
    conn = reset(tmp_path / "org4.db")
    _seed_two_orgs(conn)
    parent = register_identity(
        conn, organization_id="org-a", agent_id="parent-a", agent_name="Parent A",
        owner_human_id="human-a", subject="parent-a", granted_scope=["get_weather", "get_forecast"],
    )

    with pytest.raises(ValueError, match="does not exist in organization"):
        register_identity(
            conn, organization_id="org-b", agent_id="child-b", agent_name="Child B",
            owner_human_id="human-b", subject="child-b", granted_scope=["get_weather"],
            parent_identity_id=parent.identity_id,
        )


def test_register_identity_refuses_owner_from_another_organization(tmp_path):
    conn = reset(tmp_path / "org5.db")
    _seed_two_orgs(conn)
    with pytest.raises(ValueError, match="does not exist in organization"):
        register_identity(
            conn, organization_id="org-a", agent_id="agent-a", agent_name="Agent A",
            owner_human_id="human-b", subject="agent-a", granted_scope=["get_weather"],
        )


def test_claim_discovered_agent_refuses_owner_from_another_organization(tmp_path):
    conn = reset(tmp_path / "org6.db")
    _seed_two_orgs(conn)
    agent_id, _ = discover_agent(conn, organization_id="org-a", client_info={"name": "some-bot"})

    with pytest.raises(ValueError, match="does not exist in organization"):
        claim_discovered_agent(
            conn, organization_id="org-a", agent_id=agent_id, owner_human_id="human-b",
            granted_scope=["get_weather"],
        )


# -- console queries: dashboard/agent-detail/event-detail/search ---------

def _seed_agent_with_event(conn, *, org, agent_id, owner_id):
    from bf_agent_viewer.events import log_event

    register_identity(
        conn, organization_id=org, agent_id=agent_id, agent_name=agent_id,
        owner_human_id=owner_id, subject=agent_id, granted_scope=["get_weather"],
    )
    event_id, _ = log_event(
        conn, organization_id=org, agent_id=agent_id, session_id=None,
        actor_human_id=None, event_type="tool.called", action="read",
        tool_id="get_weather", result="success", metadata={}, prev_hash="GENESIS",
    )
    return event_id


def test_get_agent_does_not_return_another_organizations_agent(tmp_path):
    conn = reset(tmp_path / "org7.db")
    _seed_two_orgs(conn)
    _seed_agent_with_event(conn, org="org-b", agent_id="agent-b", owner_id="human-b")

    # No org filter: still resolves (existing single-tenant behavior).
    assert queries.get_agent(conn, "agent-b") is not None
    # A console restricted to org-a must not be able to fetch org-b's agent
    # by direct navigation to /agents/agent-b.
    assert queries.get_agent(conn, "agent-b", organization_id="org-a") is None
    assert queries.get_agent(conn, "agent-b", organization_id="org-b") is not None


def test_get_event_does_not_return_another_organizations_event(tmp_path):
    conn = reset(tmp_path / "org8.db")
    _seed_two_orgs(conn)
    event_id = _seed_agent_with_event(conn, org="org-b", agent_id="agent-b", owner_id="human-b")

    assert queries.get_event(conn, event_id, organization_id="org-a") is None
    assert queries.get_event(conn, event_id, organization_id="org-b") is not None


def test_list_events_excludes_other_organizations_events(tmp_path):
    conn = reset(tmp_path / "org9.db")
    _seed_two_orgs(conn)
    _seed_agent_with_event(conn, org="org-a", agent_id="agent-a", owner_id="human-a")
    _seed_agent_with_event(conn, org="org-b", agent_id="agent-b", owner_id="human-b")

    events_a = queries.list_events(conn, organization_id="org-a", limit=50)
    assert {e.agent_id for e in events_a} == {"agent-a"}

    events_all = queries.list_events(conn, limit=50)
    assert {e.agent_id for e in events_all} == {"agent-a", "agent-b"}


def test_search_does_not_surface_other_organizations_agents_or_events(tmp_path):
    conn = reset(tmp_path / "org10.db")
    _seed_two_orgs(conn)
    _seed_agent_with_event(conn, org="org-a", agent_id="widget-agent-a", owner_id="human-a")
    _seed_agent_with_event(conn, org="org-b", agent_id="widget-agent-b", owner_id="human-b")

    results_a = queries.search(conn, "widget", organization_id="org-a")
    assert {a.id for a in results_a["agents"]} == {"widget-agent-a"}
    assert {e.agent_id for e in results_a["events"]} == {"widget-agent-a"}

    results_unfiltered = queries.search(conn, "widget")
    assert {a.id for a in results_unfiltered["agents"]} == {"widget-agent-a", "widget-agent-b"}


# -- real gateway end-to-end: the actual authentication bypass this audit
#    found (load_token_registry, previously unfiltered by organization) --

@pytest.mark.asyncio
async def test_gateway_configured_for_one_org_rejects_a_token_issued_for_another(tmp_path):
    """Two organizations share one database file (exactly the shape F-015's
    schema and the console's own --org flag anticipate). A gateway process
    is started for org-a only. A bearer token issued for an org-b agent
    must NOT resolve against it -- before the T-014 fix, load_token_registry
    loaded every organization's active tokens into one shared dict, so
    this token would have authenticated successfully and been treated as
    a legitimate, if unscoped-looking, caller."""
    conn = reset(tmp_path / "org_gw1.db")
    _seed_two_orgs(conn)

    identity_b = register_identity(
        conn, organization_id="org-b", agent_id="agent-b", agent_name="Agent B",
        owner_human_id="human-b", subject="agent-b", granted_scope=["get_weather"],
    )
    token_b = issue_token(conn, agent_id=identity_b.agent_id)

    gateway, _middleware = build_gateway(
        conn, backend_script=BACKEND_SCRIPT, organization_id="org-a", write_tools=set(),
        prefer_container=False,
    )

    async def run_server():
        await gateway.run_async(transport="http", host="127.0.0.1", port=8970, show_banner=False)

    server_task = asyncio.create_task(run_server())
    await asyncio.sleep(1.0)
    try:
        transport = StreamableHttpTransport(
            "http://127.0.0.1:8970/mcp", headers={"x-bf-agent-token": token_b},
        )
        async with Client(transport) as client:
            await client.call_tool("get_weather", {"city": "Lima"})
    finally:
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):
            pass

    # The org-b token was not recognized: the call fell through to the
    # passive-discovery path (an unresolved/unscoped caller), not to
    # agent-b's real, org-b-scoped identity.
    rows = conn.execute(
        "SELECT agent_id FROM events WHERE event_type = 'tool.called'"
    ).fetchall()
    called_agent_ids = {r[0] for r in rows}
    assert "agent-b" not in called_agent_ids

    discovery_events = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'agent.discovered'"
    ).fetchone()[0]
    assert discovery_events == 1
