"""F-048/T-039: real integration test for the passive-discovery alert.
discover_agent()'s first_sighting flag already gates the one-time
agent.discovered event (test_discovery_gateway_integration.py,
test_otel_ingest.py) -- this checks the alert wired off that same flag
actually fires, exactly once per newly-seen agent, through a real
gateway process and (separately) the real OTel ingestion route, not
just that the underlying discovery mechanism works."""
import asyncio
import json
import os
import urllib.error
import urllib.request

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from bf_agent_viewer.alerts import Alert
from bf_agent_viewer.db import reset
from bf_agent_viewer.gateway import build_gateway
from bf_agent_viewer.sdk import OTEL_INGEST_PATH

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_SCRIPT = os.path.join(HERE, "fixtures", "demo_backend.py")


class CapturingAlertChannel:
    def __init__(self):
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


async def _call_with_headers(port, headers, tool_name, arguments=None):
    transport = StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp", headers=headers)
    async with Client(transport) as client:
        return await client.call_tool(tool_name, arguments or {})


@pytest.mark.asyncio
async def test_first_sighting_of_an_unrecognized_caller_fires_a_discovery_alert(tmp_path):
    conn = reset(tmp_path / "disc_alert1.db")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")

    channel = CapturingAlertChannel()
    gateway, _middleware = build_gateway(
        conn, backend_script=BACKEND_SCRIPT, organization_id="org-1", write_tools=set(),
        alert_channel=channel, prefer_container=False,
    )

    async def run_server():
        await gateway.run_async(transport="http", host="127.0.0.1", port=8993, show_banner=False)

    server_task = asyncio.create_task(run_server())
    await asyncio.sleep(1.0)
    try:
        # No X-BF-Agent-Token header at all -- an entirely unrecognized caller.
        await _call_with_headers(8993, {}, "get_weather", {"city": "Lima"})
    finally:
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    assert len(channel.sent) == 1
    assert channel.sent[0].alert_type == "agent_discovered"
    assert channel.sent[0].severity == "info"

    row = conn.execute(
        "SELECT alert_type, severity, delivered FROM alerts WHERE organization_id = 'org-1'"
    ).fetchone()
    assert row == ("agent_discovered", "info", 1)


@pytest.mark.asyncio
async def test_repeated_calls_from_the_same_unrecognized_caller_only_fire_one_alert(tmp_path):
    conn = reset(tmp_path / "disc_alert2.db")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")

    channel = CapturingAlertChannel()
    gateway, _middleware = build_gateway(
        conn, backend_script=BACKEND_SCRIPT, organization_id="org-1", write_tools=set(),
        alert_channel=channel, prefer_container=False,
    )

    async def run_server():
        await gateway.run_async(transport="http", host="127.0.0.1", port=8994, show_banner=False)

    server_task = asyncio.create_task(run_server())
    await asyncio.sleep(1.0)
    try:
        transport = StreamableHttpTransport("http://127.0.0.1:8994/mcp", headers={})
        async with Client(transport) as client:
            await client.call_tool("get_weather", {"city": "Lima"})
            await client.call_tool("get_weather", {"city": "Quito"})
    finally:
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    # Same first-sighting-only discipline agent.discovered already has --
    # the second call from the same now-known caller must not fire another.
    assert len(channel.sent) == 1


@pytest.mark.asyncio
async def test_a_presented_but_unrecognized_token_still_fires_a_discovery_alert(tmp_path):
    """F-016/T-024's rejection path (a revoked/expired/bogus token) falls
    back to the same discover_agent() first-sighting mechanism when the
    token doesn't match any in-org credential -- confirming the alert
    fires there too, not only on the plain no-token path."""
    conn = reset(tmp_path / "disc_alert3.db")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")

    channel = CapturingAlertChannel()
    gateway, _middleware = build_gateway(
        conn, backend_script=BACKEND_SCRIPT, organization_id="org-1", write_tools=set(),
        alert_channel=channel, prefer_container=False,
    )

    async def run_server():
        await gateway.run_async(transport="http", host="127.0.0.1", port=8995, show_banner=False)

    server_task = asyncio.create_task(run_server())
    await asyncio.sleep(1.0)
    try:
        with pytest.raises(Exception):  # noqa: BLE001 -- rejected, per F-016/T-024
            await _call_with_headers(8995, {"X-BF-Agent-Token": "bfav_bogus_never_issued"}, "get_weather", {"city": "Lima"})
    finally:
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    assert len(channel.sent) == 1
    assert channel.sent[0].alert_type == "agent_discovered"


def _post_raw_sync(gateway_url: str, payload: dict, *, token: str | None = None):
    """Same shape as test_otel_ingest.py's own helper -- a single
    event dict with top-level "result"/"attributes", not a batch --
    duplicated here rather than imported since it's a small,
    file-local test utility there too."""
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-BF-Agent-Token"] = token
    request = urllib.request.Request(
        f"{gateway_url.rstrip('/')}{OTEL_INGEST_PATH}",
        data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


async def _post_raw(*args, **kwargs):
    return await asyncio.to_thread(_post_raw_sync, *args, **kwargs)


@pytest.mark.asyncio
async def test_otel_ingestion_path_also_fires_a_discovery_alert_on_first_sighting(tmp_path):
    """Same alert, same alert_type, off the OTel SDK fallback path's own
    first_sighting -- one visibility guarantee shared by both
    instrumentation paths, not something only the MCP gateway got."""
    conn = reset(tmp_path / "disc_alert4.db")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")

    channel = CapturingAlertChannel()
    gateway, _middleware = build_gateway(
        conn, backend_script=BACKEND_SCRIPT, organization_id="org-1", write_tools=set(),
        alert_channel=channel, prefer_container=False,
    )

    async def run_server():
        await gateway.run_async(transport="http", host="127.0.0.1", port=8996, show_banner=False)

    server_task = asyncio.create_task(run_server())
    await asyncio.sleep(1.0)
    try:
        status, body = await _post_raw(
            "http://127.0.0.1:8996",
            {
                "result": "success",
                "attributes": {
                    "gen_ai.tool.name": "send_invoice",
                    "gen_ai.agent.name": "billing-agent",
                },
            },
            token=None,
        )
        assert status == 202, body
    finally:
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    assert len(channel.sent) == 1
    assert channel.sent[0].alert_type == "agent_discovered"
