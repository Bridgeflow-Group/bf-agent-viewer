"""--require-token: a revoked/scoped agent must not regain access by simply
omitting its credential header. Without the flag (default) that bypass
exists -- the first test pins that documented behaviour so a change to the
default is a deliberate decision, not an accident."""
import asyncio
import os

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from bf_agent_viewer.db import reset
from bf_agent_viewer.gateway import build_gateway
from bf_agent_viewer.identity import issue_token, register_identity, revoke_token

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_SCRIPT = os.path.join(HERE, "fixtures", "demo_backend.py")
PORT_OPEN, PORT_STRICT = 9071, 9072


def _setup(tmp_path, *, require_token):
    conn = reset(tmp_path / "gw.db")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('human-1', 'org-1', 'Owner')")
    register_identity(conn, organization_id="org-1", agent_id="agent-a", agent_name="A",
                      owner_human_id="human-1", subject="agent-a", granted_scope=["get_weather"])
    token = issue_token(conn, agent_id="agent-a")
    gateway, mw = build_gateway(conn, backend_script=BACKEND_SCRIPT, organization_id="org-1",
                                write_tools=set(), prefer_container=False,
                                credential_refresh_seconds=1.0, require_token=require_token)
    return conn, gateway, mw, token


async def _call(port, token=None):
    headers = {"X-BF-Agent-Token": token} if token else {}
    async with Client(StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp", headers=headers)) as c:
        return await c.call_tool("get_weather", {"city": "Lima"})


async def _run(gateway, port, body):
    task = asyncio.create_task(gateway.run_async(transport="http", host="127.0.0.1", port=port, show_banner=False))
    await asyncio.sleep(1.0)
    try:
        await body()
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_default_lets_a_tokenless_caller_through_after_revocation(tmp_path):
    conn, gateway, mw, token = _setup(tmp_path, require_token=False)

    async def body():
        revoke_token(conn, token)
        await asyncio.sleep(1.5)
        with pytest.raises(Exception):
            await _call(PORT_OPEN, token)          # revoked token: rejected
        await _call(PORT_OPEN)                       # no token: bypass (documented gap)
    await _run(gateway, PORT_OPEN, body)


@pytest.mark.asyncio
async def test_require_token_rejects_tokenless_caller_and_logs_it(tmp_path):
    conn, gateway, mw, token = _setup(tmp_path, require_token=True)

    async def body():
        await _call(PORT_STRICT, token)              # valid token still works
        with pytest.raises(Exception):
            await _call(PORT_STRICT)                 # no token: rejected
        revoke_token(conn, token)
        await asyncio.sleep(1.5)
        with pytest.raises(Exception):
            await _call(PORT_STRICT)                 # still rejected after revoke
        n = conn.execute("SELECT COUNT(*) FROM events WHERE event_type='tool.rejected_no_token'").fetchone()[0]
        assert n >= 2
    await _run(gateway, PORT_STRICT, body)
