"""F-047: agent ping / health check, resolved (OQ-026) as an agent-side
heartbeat rather than a platform-initiated probe -- MCP's stateless
per-request model (OQ-015) leaves no persistent session to probe, and
agents only ever connect outward through the gateway, so there's no
inbound path to actually "ping" one. Real gateway, real FastMCP client,
real HTTP -- not mocked."""
import asyncio
import os

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from bf_agent_viewer.db import reset
from bf_agent_viewer.events import last_hash
from bf_agent_viewer.gateway import build_gateway
from bf_agent_viewer.gateway.middleware import HEARTBEAT_TOOL_NAME
from bf_agent_viewer.identity import issue_token, register_identity
from bf_agent_viewer.ratelimit import TokenBucketLimiter

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_SCRIPT = os.path.join(HERE, "fixtures", "demo_backend.py")


async def _run_gateway_and_call(conn, *, port, token, tool_name, arguments=None, rate_limiter=None):
    gateway, middleware = build_gateway(
        conn, backend_script=BACKEND_SCRIPT, organization_id="org-1", write_tools=set(),
        rate_limiter=rate_limiter, prefer_container=False,
    )

    async def run_server():
        await gateway.run_async(transport="http", host="127.0.0.1", port=port, show_banner=False)

    server_task = asyncio.create_task(run_server())
    await asyncio.sleep(1.0)
    try:
        transport = StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp", headers={"X-BF-Agent-Token": token})
        async with Client(transport) as client:
            result = await client.call_tool(tool_name, arguments or {})
    finally:
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):
            pass
    return result, middleware


@pytest.mark.asyncio
async def test_heartbeat_call_succeeds_and_is_not_proxied_to_backend(tmp_path):
    conn = reset(tmp_path / "hb1.db")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('human-1', 'org-1', 'Owner')")
    register_identity(
        conn, organization_id="org-1", agent_id="agent-1", agent_name="Agent",
        owner_human_id="human-1", subject="agent-1", granted_scope=["get_weather"],
    )
    token = issue_token(conn, agent_id="agent-1")

    result, _middleware = await _run_gateway_and_call(
        conn, port=8950, token=token, tool_name=HEARTBEAT_TOOL_NAME,
    )
    assert result.data == {"status": "ok"}


@pytest.mark.asyncio
async def test_heartbeat_logs_a_distinct_event_type_not_tool_called(tmp_path):
    conn = reset(tmp_path / "hb2.db")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('human-1', 'org-1', 'Owner')")
    register_identity(
        conn, organization_id="org-1", agent_id="agent-1", agent_name="Agent",
        owner_human_id="human-1", subject="agent-1", granted_scope=["get_weather"],
    )
    token = issue_token(conn, agent_id="agent-1")

    await _run_gateway_and_call(conn, port=8951, token=token, tool_name=HEARTBEAT_TOOL_NAME)

    row = conn.execute(
        "SELECT event_type, agent_id, tool_id, result FROM events WHERE agent_id = 'agent-1'"
    ).fetchone()
    assert row == ("agent.heartbeat", "agent-1", HEARTBEAT_TOOL_NAME, "success")


@pytest.mark.asyncio
async def test_heartbeat_is_not_scope_checked(tmp_path):
    """The agent's granted_scope only ever includes get_weather -- a
    heartbeat call must succeed anyway, since it isn't a real capability
    and was never meant to be part of anyone's allowlist."""
    conn = reset(tmp_path / "hb3.db")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('human-1', 'org-1', 'Owner')")
    register_identity(
        conn, organization_id="org-1", agent_id="agent-1", agent_name="Agent",
        owner_human_id="human-1", subject="agent-1", granted_scope=["get_weather"],
    )
    token = issue_token(conn, agent_id="agent-1")

    result, _ = await _run_gateway_and_call(conn, port=8952, token=token, tool_name=HEARTBEAT_TOOL_NAME)
    assert result.data == {"status": "ok"}

    blocked_row = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'tool.blocked'"
    ).fetchone()
    assert blocked_row == (0,)


@pytest.mark.asyncio
async def test_heartbeat_bypasses_rate_limit(tmp_path):
    """A burst-1 limiter would reject a second real tool call -- a second
    heartbeat right after must still succeed, since heartbeats shouldn't
    compete with real work for the same budget."""
    conn = reset(tmp_path / "hb4.db")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('human-1', 'org-1', 'Owner')")
    register_identity(
        conn, organization_id="org-1", agent_id="agent-1", agent_name="Agent",
        owner_human_id="human-1", subject="agent-1", granted_scope=["get_weather"],
    )
    token = issue_token(conn, agent_id="agent-1")
    limiter = TokenBucketLimiter(rate=0.001, burst=1)

    gateway, _middleware = build_gateway(
        conn, backend_script=BACKEND_SCRIPT, organization_id="org-1", write_tools=set(),
        rate_limiter=limiter, prefer_container=False,
    )

    async def run_server():
        await gateway.run_async(transport="http", host="127.0.0.1", port=8953, show_banner=False)

    server_task = asyncio.create_task(run_server())
    await asyncio.sleep(1.0)
    try:
        transport = StreamableHttpTransport("http://127.0.0.1:8953/mcp", headers={"X-BF-Agent-Token": token})
        async with Client(transport) as client:
            # Exhaust the burst-1 allocation on a real tool call.
            await client.call_tool("get_weather", {"city": "Lima"})
            # Heartbeats right after must still succeed -- not rate-limited.
            r1 = await client.call_tool(HEARTBEAT_TOOL_NAME, {})
            r2 = await client.call_tool(HEARTBEAT_TOOL_NAME, {})
    finally:
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):
            pass

    assert r1.data == {"status": "ok"}
    assert r2.data == {"status": "ok"}
    heartbeat_count = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'agent.heartbeat'"
    ).fetchone()
    assert heartbeat_count == (2,)


@pytest.mark.asyncio
async def test_heartbeat_extends_the_tamper_evident_chain(tmp_path):
    conn = reset(tmp_path / "hb5.db")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('human-1', 'org-1', 'Owner')")
    register_identity(
        conn, organization_id="org-1", agent_id="agent-1", agent_name="Agent",
        owner_human_id="human-1", subject="agent-1", granted_scope=["get_weather"],
    )
    token = issue_token(conn, agent_id="agent-1")
    tip_before = last_hash(conn)

    _, middleware = await _run_gateway_and_call(conn, port=8954, token=token, tool_name=HEARTBEAT_TOOL_NAME)

    assert middleware.running_hash != tip_before
    assert middleware.running_hash == last_hash(conn)
