"""F-024/T-019: the OTel-SDK fallback ingestion path. Real gateway, real
HTTP, real bf_agent_viewer.sdk.BFAgentViewerClient making real requests --
not mocked, matching this project's own standard (see test_heartbeat.py,
test_gateway_integration.py) of proving the client and server sides
actually work together rather than testing each in isolation against
assumptions about the other."""
import asyncio
import json
import os
import urllib.error
import urllib.request

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from bf_agent_viewer.db import reset
from bf_agent_viewer.events import last_hash, verify_chain
from bf_agent_viewer.gateway import build_gateway
from bf_agent_viewer.identity import issue_token, register_identity
from bf_agent_viewer.ratelimit import TokenBucketLimiter
from bf_agent_viewer.sdk import OTEL_INGEST_PATH, BFAgentViewerClient, BFAgentViewerReportError

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_SCRIPT = os.path.join(HERE, "fixtures", "demo_backend.py")


def _seed_org_and_human(conn):
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('human-1', 'org-1', 'Owner')")


async def _run_gateway(conn, *, port, write_tools=None, rate_limiter=None):
    gateway, middleware = build_gateway(
        conn, backend_script=BACKEND_SCRIPT, organization_id="org-1",
        write_tools=write_tools or set(), rate_limiter=rate_limiter, prefer_container=False,
    )
    server_task = asyncio.create_task(
        gateway.run_async(transport="http", host="127.0.0.1", port=port, show_banner=False)
    )
    await asyncio.sleep(1.0)
    return server_task, middleware


async def _stop(server_task):
    server_task.cancel()
    try:
        await server_task
    except (asyncio.CancelledError, Exception):
        pass


async def _record(client, *args, **kwargs):
    """bf_agent_viewer.sdk.BFAgentViewerClient is a deliberately
    synchronous, blocking client (see sdk.py's own module docstring --
    that's the right design for the non-async agent code it targets).
    But the gateway under test in this file runs on the SAME asyncio
    event loop as the test itself, so calling it directly here would
    block that one loop while it's also the thing that has to run the
    server -- a self-deadlock that is purely a test-harness artifact, not
    a real client/server issue (a real caller runs in its own process).
    asyncio.to_thread runs the blocking call on a separate thread so the
    event loop stays free to actually serve the request."""
    return await asyncio.to_thread(client.record_tool_call, *args, **kwargs)


async def _post(client, *args, **kwargs):
    return await asyncio.to_thread(client._post, *args, **kwargs)


def _post_raw_sync(gateway_url, payload, *, token=None):
    """Bypasses BFAgentViewerClient entirely -- its `token` field is a
    required str, so it can't express "no token presented at all," only
    "some token string." Genuine no-token passive discovery (F-016/T-024
    -- must stay unchanged, distinct from a presented-but-unrecognized
    token, which is now rejected) needs a raw request with the auth
    header omitted, not just an empty/placeholder value passed as one."""
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
async def test_registered_agent_reports_a_successful_tool_call(tmp_path):
    conn = reset(tmp_path / "otel1.db")
    _seed_org_and_human(conn)
    register_identity(
        conn, organization_id="org-1", agent_id="agent-1", agent_name="Agent",
        owner_human_id="human-1", subject="agent-1", granted_scope=["send_invoice"],
    )
    token = issue_token(conn, agent_id="agent-1")

    server_task, _middleware = await _run_gateway(conn, port=8980)
    try:
        client = BFAgentViewerClient(
            gateway_url="http://127.0.0.1:8980", token=token, agent_name="agent-1",
            raise_on_error=True,
        )
        await _record(
            client, "send_invoice", arguments={"customer": "acme"}, result="success", duration_ms=42.5,
        )
    finally:
        await _stop(server_task)

    row = conn.execute(
        "SELECT event_type, agent_id, tool_id, result, source, action FROM events "
        "WHERE tool_id = 'send_invoice'"
    ).fetchone()
    assert row == ("tool.called", "agent-1", "send_invoice", "success", "otel_sdk", "read")


@pytest.mark.asyncio
async def test_write_tool_is_classified_as_a_write_action(tmp_path):
    conn = reset(tmp_path / "otel2.db")
    _seed_org_and_human(conn)
    register_identity(
        conn, organization_id="org-1", agent_id="agent-1", agent_name="Agent",
        owner_human_id="human-1", subject="agent-1", granted_scope=["send_invoice"],
    )
    token = issue_token(conn, agent_id="agent-1")

    server_task, _ = await _run_gateway(conn, port=8981, write_tools={"send_invoice"})
    try:
        client = BFAgentViewerClient(
            gateway_url="http://127.0.0.1:8981", token=token, raise_on_error=True,
        )
        await _record(client, "send_invoice", result="success")
    finally:
        await _stop(server_task)

    row = conn.execute("SELECT action FROM events WHERE tool_id = 'send_invoice'").fetchone()
    assert row == ("write",)


@pytest.mark.asyncio
async def test_reported_error_is_logged_with_error_type_and_result(tmp_path):
    conn = reset(tmp_path / "otel3.db")
    _seed_org_and_human(conn)
    register_identity(
        conn, organization_id="org-1", agent_id="agent-1", agent_name="Agent",
        owner_human_id="human-1", subject="agent-1", granted_scope=["send_invoice"],
    )
    token = issue_token(conn, agent_id="agent-1")

    server_task, _ = await _run_gateway(conn, port=8982)

    def do_traced_call():
        client = BFAgentViewerClient(
            gateway_url="http://127.0.0.1:8982", token=token, raise_on_error=True,
        )
        with client.trace_tool_call("send_invoice", arguments={"customer": "acme"}):
            raise ValueError("customer not found")

    try:
        with pytest.raises(ValueError):
            await asyncio.to_thread(do_traced_call)
    finally:
        await _stop(server_task)

    row = conn.execute(
        "SELECT result, metadata FROM events WHERE tool_id = 'send_invoice'"
    ).fetchone()
    assert row[0] == "error"
    import json
    metadata = json.loads(row[1])
    assert metadata["error_type"] == "ValueError"
    assert metadata["duration_ms"] is not None


@pytest.mark.asyncio
async def test_unregistered_caller_with_no_token_is_passively_discovered(tmp_path):
    """Genuinely no token presented at all -- the one case that still gets
    passive-discovery treatment (F-016/T-024 narrowed this from "any
    unrecognized token" down to just this). client_info-based discovery
    here keys off gen_ai.agent.name in the event's own attributes (see
    otel_ingest.py), not a real MCP clientInfo negotiation, so the agent
    name has to travel in the payload itself rather than via
    BFAgentViewerClient's agent_name field -- hence the raw payload
    instead of the SDK client for this one test."""
    conn = reset(tmp_path / "otel4.db")
    _seed_org_and_human(conn)

    server_task, _ = await _run_gateway(conn, port=8983)
    try:
        status, body = await _post_raw(
            "http://127.0.0.1:8983",
            {
                "result": "success",
                "attributes": {
                    "gen_ai.tool.name": "send_invoice",
                    "gen_ai.agent.name": "billing-script",
                },
            },
            token=None,
        )
    finally:
        await _stop(server_task)

    assert status == 202, body

    agent_row = conn.execute(
        "SELECT id, status, name FROM agents WHERE id = 'discovered-billing-script'"
    ).fetchone()
    assert agent_row == ("discovered-billing-script", "unclaimed", "billing-script")

    discovered_event = conn.execute(
        "SELECT event_type FROM events WHERE agent_id = 'discovered-billing-script' "
        "AND event_type = 'agent.discovered'"
    ).fetchone()
    assert discovered_event is not None

    called_event = conn.execute(
        "SELECT event_type, tool_id FROM events WHERE agent_id = 'discovered-billing-script' "
        "AND event_type = 'tool.called'"
    ).fetchone()
    assert called_event == ("tool.called", "send_invoice")


@pytest.mark.asyncio
async def test_unrecognized_presented_token_is_rejected_not_discovered(tmp_path):
    """F-016/T-024: the other half of the same distinction -- a token that
    WAS presented but doesn't resolve to anything (bogus, revoked,
    expired) must be rejected outright, not folded into passive discovery
    the way it was before this task. Otherwise revoking a credential (or
    letting a short-lived one expire) would leave the caller no worse off
    than an anonymous visitor -- which, on the MCP path, is actually
    *unscoped*, strictly more permissive than what a revoked identity had.
    This endpoint can't grant tool-call capability either way (see the
    module docstring), but the rejection still needs to be real here too,
    so a revoked identity's reports don't quietly get reclassified as
    unscoped instead of refused."""
    conn = reset(tmp_path / "otel4.db")
    _seed_org_and_human(conn)

    server_task, _ = await _run_gateway(conn, port=8988)
    try:
        status, body = await _post_raw(
            "http://127.0.0.1:8988",
            {
                "result": "success",
                "attributes": {
                    "gen_ai.tool.name": "send_invoice",
                    "gen_ai.agent.name": "billing-script",
                },
            },
            token="not-a-real-token",
        )
    finally:
        await _stop(server_task)

    assert status == 401, body
    assert conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_out_of_scope_call_is_logged_and_alerted_but_not_blocked(tmp_path):
    """This endpoint can't block anything -- the real call already
    happened out-of-band. It should still log it (visibility) and fire an
    alert (something worth a human noticing), never silently drop it and
    never pretend it was prevented."""
    conn = reset(tmp_path / "otel5.db")
    _seed_org_and_human(conn)
    register_identity(
        conn, organization_id="org-1", agent_id="agent-1", agent_name="Agent",
        owner_human_id="human-1", subject="agent-1", granted_scope=["get_weather"],
    )
    token = issue_token(conn, agent_id="agent-1")

    server_task, _ = await _run_gateway(conn, port=8984)
    try:
        client = BFAgentViewerClient(
            gateway_url="http://127.0.0.1:8984", token=token, raise_on_error=True,
        )
        # send_invoice is NOT in agent-1's granted_scope (only get_weather is).
        await _record(client, "send_invoice", result="success")
    finally:
        await _stop(server_task)

    row = conn.execute(
        "SELECT result, metadata FROM events WHERE tool_id = 'send_invoice'"
    ).fetchone()
    import json
    assert row[0] == "success"  # logged as what actually happened, not blocked
    metadata = json.loads(row[1])
    assert metadata["scope_check"] == "outside granted_scope"

    alert_row = conn.execute(
        "SELECT alert_type, severity FROM alerts WHERE agent_id = 'agent-1'"
    ).fetchone()
    assert alert_row == ("otel_scope_violation_reported", "warning")


@pytest.mark.asyncio
async def test_ingestion_rate_limit_protects_the_pipeline_not_the_call(tmp_path):
    """A burst-1 limiter rejects the second POST outright (429) -- this
    only protects the platform's own event log from being flooded, it
    cannot and does not claim to have stopped whatever tool call the
    rejected report was describing (that already happened)."""
    conn = reset(tmp_path / "otel6.db")
    _seed_org_and_human(conn)
    register_identity(
        conn, organization_id="org-1", agent_id="agent-1", agent_name="Agent",
        owner_human_id="human-1", subject="agent-1", granted_scope=["send_invoice"],
    )
    token = issue_token(conn, agent_id="agent-1")
    limiter = TokenBucketLimiter(rate=0.001, burst=1)

    server_task, _ = await _run_gateway(conn, port=8985, rate_limiter=limiter)
    try:
        client = BFAgentViewerClient(
            gateway_url="http://127.0.0.1:8985", token=token, raise_on_error=True,
        )
        await _record(client, "send_invoice", result="success")
        with pytest.raises(BFAgentViewerReportError):
            await _record(client, "send_invoice", result="success")
    finally:
        await _stop(server_task)

    count = conn.execute("SELECT COUNT(*) FROM events WHERE tool_id = 'send_invoice'").fetchone()
    assert count == (1,)


@pytest.mark.asyncio
async def test_malformed_event_is_rejected_with_400_not_silently_dropped(tmp_path):
    conn = reset(tmp_path / "otel7.db")
    _seed_org_and_human(conn)
    register_identity(
        conn, organization_id="org-1", agent_id="agent-1", agent_name="Agent",
        owner_human_id="human-1", subject="agent-1", granted_scope=["send_invoice"],
    )
    token = issue_token(conn, agent_id="agent-1")

    server_task, _ = await _run_gateway(conn, port=8986)
    try:
        client = BFAgentViewerClient(
            gateway_url="http://127.0.0.1:8986", token=token, raise_on_error=True,
        )
        # No "gen_ai.tool.name" -- the one field this endpoint requires.
        with pytest.raises(BFAgentViewerReportError):
            await _post(client, {"events": [{"attributes": {}}]})
    finally:
        await _stop(server_task)

    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()
    assert count == (0,)


@pytest.mark.asyncio
async def test_otel_events_extend_the_same_chain_as_mcp_events(tmp_path):
    """Both instrumentation paths must write into one coherent tamper-
    evident history, not fork into two -- an OTel-reported event and an
    MCP-proxied call from the same gateway process chain together, and
    the whole thing still verifies."""
    conn = reset(tmp_path / "otel8.db")
    _seed_org_and_human(conn)
    register_identity(
        conn, organization_id="org-1", agent_id="agent-1", agent_name="Agent",
        owner_human_id="human-1", subject="agent-1", granted_scope=["get_weather", "send_invoice"],
    )
    token = issue_token(conn, agent_id="agent-1")

    gateway, middleware = build_gateway(
        conn, backend_script=BACKEND_SCRIPT, organization_id="org-1",
        write_tools=set(), prefer_container=False,
    )
    server_task = asyncio.create_task(
        gateway.run_async(transport="http", host="127.0.0.1", port=8987, show_banner=False)
    )
    await asyncio.sleep(1.0)
    try:
        # A real MCP tool call first.
        transport = StreamableHttpTransport(
            "http://127.0.0.1:8987/mcp", headers={"X-BF-Agent-Token": token},
        )
        async with Client(transport) as mcp_client:
            await mcp_client.call_tool("get_weather", {"city": "Lima"})

        # Then a real OTel-SDK report on the same gateway process.
        otel_client = BFAgentViewerClient(
            gateway_url="http://127.0.0.1:8987", token=token, raise_on_error=True,
        )
        await _record(otel_client, "send_invoice", result="success")
    finally:
        await _stop(server_task)

    ok, bad_id = verify_chain(conn)
    assert ok, f"chain verification failed at {bad_id}"
    assert middleware.running_hash == last_hash(conn)

    event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()
    assert event_count == (2,)
