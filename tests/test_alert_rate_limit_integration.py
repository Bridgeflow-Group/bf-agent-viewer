"""Real integration test for F-036's first actual consumer: security.md's
stated promise that "a rejection itself is surfaced as an alert" when the
per-agent rate limit is exceeded. Runs a real gateway process, makes real
calls through a real FastMCP client until the limiter rejects one, and
checks both the persisted `alerts` row and that the configured channel
was actually invoked -- not just that the rate limit itself works
(test_ratelimit.py and test_gateway_integration.py already cover that)."""
import asyncio
import os

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from bf_agent_viewer.alerts import Alert
from bf_agent_viewer.db import reset
from bf_agent_viewer.gateway import build_gateway
from bf_agent_viewer.identity import issue_token, register_identity
from bf_agent_viewer.ratelimit import TokenBucketLimiter

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_SCRIPT = os.path.join(HERE, "fixtures", "demo_backend.py")


class CapturingAlertChannel:
    def __init__(self):
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


@pytest.mark.asyncio
async def test_rate_limit_rejection_fires_and_persists_an_alert(tmp_path):
    conn = reset(tmp_path / "gw_alert.db")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('human-1', 'org-1', 'Owner')")
    identity = register_identity(
        conn, organization_id="org-1", agent_id="agent-1", agent_name="Agent",
        owner_human_id="human-1", subject="agent-1", granted_scope=["get_weather"],
    )
    token = issue_token(conn, agent_id="agent-1")

    channel = CapturingAlertChannel()
    # Burst of 1: the second call in quick succession is guaranteed to be
    # rejected, without a real-time sleep-based flake.
    limiter = TokenBucketLimiter(rate=0.001, burst=1)

    gateway, _middleware = build_gateway(
        conn, backend_script=BACKEND_SCRIPT, organization_id="org-1", write_tools=set(),
        rate_limiter=limiter, alert_channel=channel, prefer_container=False,
    )

    async def run_server():
        await gateway.run_async(transport="http", host="127.0.0.1", port=8946, show_banner=False)

    server_task = asyncio.create_task(run_server())
    await asyncio.sleep(1.0)

    try:
        transport = StreamableHttpTransport("http://127.0.0.1:8946/mcp", headers={"X-BF-Agent-Token": token})
        async with Client(transport) as client:
            await client.call_tool("get_weather", {"city": "Lima"})  # consumes the only token
            with pytest.raises(Exception):
                await client.call_tool("get_weather", {"city": "Lima"})  # rejected
    finally:
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):
            pass

    assert len(channel.sent) == 1
    assert channel.sent[0].alert_type == "rate_limited"
    assert channel.sent[0].agent_id == "agent-1"

    row = conn.execute(
        "SELECT alert_type, agent_id, delivered FROM alerts WHERE organization_id = 'org-1'"
    ).fetchone()
    assert row == ("rate_limited", "agent-1", 1)
