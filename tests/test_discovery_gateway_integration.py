"""F-027/T-012: passive discovery exercised against a real gateway process
-- an unrecognized caller (no bearer token at all) must produce a real,
visible `agents` row, not just events stamped with a placeholder id."""
import asyncio
import os

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from bf_agent_viewer.console import queries
from bf_agent_viewer.db import reset
from bf_agent_viewer.events import last_hash
from bf_agent_viewer.gateway import build_gateway
from bf_agent_viewer.identity import claim_discovered_agent, issue_token

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_SCRIPT = os.path.join(HERE, "fixtures", "demo_backend.py")


async def _call_with_headers(port, headers, tool_name, arguments=None):
    transport = StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp", headers=headers)
    async with Client(transport) as client:
        return await client.call_tool(tool_name, arguments or {})


@pytest.mark.asyncio
async def test_unrecognized_caller_gets_a_real_visible_agent_row(tmp_path):
    conn = reset(tmp_path / "disc_gw1.db")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")

    gateway, middleware = build_gateway(
        conn, backend_script=BACKEND_SCRIPT, organization_id="org-1", write_tools=set(),
        prefer_container=False,
    )

    async def run_server():
        await gateway.run_async(transport="http", host="127.0.0.1", port=8960, show_banner=False)

    server_task = asyncio.create_task(run_server())
    await asyncio.sleep(1.0)
    try:
        # No X-BF-Agent-Token header at all -- an entirely unrecognized caller.
        result = await _call_with_headers(8960, {}, "get_weather", {"city": "Lima"})
    finally:
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):
            pass

    assert result is not None

    agents = queries.list_agents(conn, organization_id="org-1")
    # Exactly one real, unowned row for the unrecognized caller -- not the
    # old fixed placeholder that never had a matching `agents` row at all.
    assert len(agents) == 1
    discovered = agents[0]
    assert discovered.owner_name is None
    assert discovered.status == "unclaimed"

    discovery_events = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'agent.discovered'"
    ).fetchone()[0]
    assert discovery_events == 1


@pytest.mark.asyncio
async def test_repeated_calls_from_the_same_unrecognized_caller_do_not_duplicate(tmp_path):
    conn = reset(tmp_path / "disc_gw2.db")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")

    gateway, _middleware = build_gateway(
        conn, backend_script=BACKEND_SCRIPT, organization_id="org-1", write_tools=set(),
        prefer_container=False,
    )

    async def run_server():
        await gateway.run_async(transport="http", host="127.0.0.1", port=8961, show_banner=False)

    server_task = asyncio.create_task(run_server())
    await asyncio.sleep(1.0)
    try:
        transport = StreamableHttpTransport("http://127.0.0.1:8961/mcp", headers={})
        async with Client(transport) as client:
            await client.call_tool("get_weather", {"city": "Lima"})
            await client.call_tool("get_weather", {"city": "Quito"})
            await client.call_tool("get_weather", {"city": "Bogota"})
    finally:
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):
            pass

    agent_count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    assert agent_count == 1
    discovery_events = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'agent.discovered'"
    ).fetchone()[0]
    assert discovery_events == 1
    tool_called_events = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'tool.called'"
    ).fetchone()[0]
    assert tool_called_events == 3


@pytest.mark.asyncio
async def test_claim_then_reconnect_with_real_token_resolves_granted_scope(tmp_path):
    """The full lifecycle: discovered anonymously -> claimed by a human,
    with a real granted scope -> a fresh connection presenting the newly
    issued token resolves to that real identity, not the unscoped
    passive-discovery path."""
    conn = reset(tmp_path / "disc_gw3.db")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('human-1', 'org-1', 'Owner')")

    gateway, middleware = build_gateway(
        conn, backend_script=BACKEND_SCRIPT, organization_id="org-1", write_tools=set(),
        prefer_container=False,
    )

    async def run_server():
        await gateway.run_async(transport="http", host="127.0.0.1", port=8962, show_banner=False)

    server_task = asyncio.create_task(run_server())
    await asyncio.sleep(1.0)
    try:
        # First contact, unrecognized.
        await _call_with_headers(8962, {}, "get_weather", {"city": "Lima"})
    finally:
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):
            pass

    discovered = queries.list_agents(conn, organization_id="org-1")
    assert len(discovered) == 1
    agent_id = discovered[0].id

    identity = claim_discovered_agent(
        conn, organization_id="org-1", agent_id=agent_id, owner_human_id="human-1",
        granted_scope=["get_weather"],
    )
    token = issue_token(conn, agent_id=identity.agent_id)

    tip_before_reconnect = last_hash(conn)

    # A fresh gateway process picks up the newly issued token (the token
    # registry is loaded at startup, per identity/registry.py's own
    # documented v0.1.0 scope).
    gateway2, middleware2 = build_gateway(
        conn, backend_script=BACKEND_SCRIPT, organization_id="org-1", write_tools=set(),
        prefer_container=False,
    )

    async def run_server2():
        await gateway2.run_async(transport="http", host="127.0.0.1", port=8963, show_banner=False)

    server_task2 = asyncio.create_task(run_server2())
    await asyncio.sleep(1.0)
    try:
        # Scoped correctly: an out-of-scope tool call must now be blocked,
        # proving this is a real, enforced identity, not the passive,
        # unscoped discovery path it started on.
        with pytest.raises(Exception):
            await _call_with_headers(8963, {"X-BF-Agent-Token": token}, "send_email", {"to": "a@b.com", "body": "hi"})
        result = await _call_with_headers(8963, {"X-BF-Agent-Token": token}, "get_weather", {"city": "Lima"})
    finally:
        server_task2.cancel()
        try:
            await server_task2
        except (asyncio.CancelledError, Exception):
            pass

    assert result is not None
    row = conn.execute(
        "SELECT event_type FROM events WHERE agent_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    assert row[0] == "tool.called"
    # No new discovery event fired for the now-claimed agent's traffic.
    discovery_events = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'agent.discovered'"
    ).fetchone()[0]
    assert discovery_events == 1
