"""Real integration test: one persistent gateway process, multiple
concurrent real connections asserting DIFFERENT identities, interleaved.

This is the exact scenario that broke the prototype's discover-time
clientInfo caching approach (found Sept 22 2026, see gateway/middleware.py
docstring) -- neither of the prototype's real-traffic validations actually
covered it: the delegation test used separate OS processes (stdio, one per
identity, so no shared-process cache collision was possible), and the
concurrent-HTTP throughput test used one shared identity for all 15
connections (no differing identities to mix up). This test is the one that
was missing -- concurrent connections, one shared process, DIFFERENT
identities and DIFFERENT scopes, checked for cross-contamination.
"""
import asyncio
import os

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from bf_agent_viewer.db import reset
from bf_agent_viewer.events import verify_chain
from bf_agent_viewer.identity import issue_token, register_identity
from bf_agent_viewer.gateway import build_gateway

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_SCRIPT = os.path.join(HERE, "fixtures", "demo_backend.py")
WRITE_TOOLS = {"send_email", "delete_record"}


@pytest.fixture
def gateway_setup(tmp_path):
    db_path = tmp_path / "gw.db"
    conn = reset(db_path)
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('human-1', 'org-1', 'Owner')")

    orchestrator = register_identity(
        conn, organization_id="org-1", agent_id="agent-orchestrator", agent_name="Orchestrator",
        owner_human_id="human-1", subject="agent-orchestrator",
        granted_scope=["get_weather", "read_file", "send_email", "delete_record"],
    )
    orchestrator_token = issue_token(conn, agent_id="agent-orchestrator")

    subagent = register_identity(
        conn, organization_id="org-1", agent_id="agent-subagent", agent_name="Sub-agent",
        owner_human_id="human-1", subject="agent-subagent",
        granted_scope=["get_weather", "read_file"],
        parent_identity_id=orchestrator.identity_id,
    )
    subagent_token = issue_token(conn, agent_id="agent-subagent")

    gateway, middleware = build_gateway(
        conn, backend_script=BACKEND_SCRIPT, organization_id="org-1", write_tools=WRITE_TOOLS,
        # This test is about identity/scope correctness, not sandboxing --
        # the container path has its own dedicated coverage in
        # test_container_sandbox.py. Forcing the rlimit-bootstrap path
        # here keeps this test from depending on the production sandbox
        # image being built/pulled (see ISS-017's container-image note).
        prefer_container=False,
    )
    return conn, gateway, orchestrator_token, subagent_token


@pytest.mark.asyncio
async def test_concurrent_interleaved_identities_no_cross_contamination(gateway_setup):
    conn, gateway, orchestrator_token, subagent_token = gateway_setup

    async def run_server():
        await gateway.run_async(transport="http", host="127.0.0.1", port=8940, show_banner=False)

    server_task = asyncio.create_task(run_server())
    await asyncio.sleep(1.0)

    try:
        async def orchestrator_calls():
            transport = StreamableHttpTransport(
                "http://127.0.0.1:8940/mcp", headers={"X-BF-Agent-Token": orchestrator_token}
            )
            results = []
            async with Client(transport) as c:
                for _ in range(10):
                    r = await c.call_tool("delete_record", {"record_id": "rec-1"})
                    results.append(r)
                    # Interleave with a sub-agent call in between, same
                    # process, so any shared-state bug would show up as
                    # cross-contamination here.
                    await asyncio.sleep(0.01)
            return results

        async def subagent_calls():
            transport = StreamableHttpTransport(
                "http://127.0.0.1:8940/mcp", headers={"X-BF-Agent-Token": subagent_token}
            )
            outcomes = []
            async with Client(transport) as c:
                for _ in range(10):
                    try:
                        await c.call_tool("delete_record", {"record_id": "rec-1"})
                        outcomes.append("allowed")
                    except Exception:  # noqa: BLE001
                        outcomes.append("blocked")
                    await asyncio.sleep(0.01)
            return outcomes

        orchestrator_results, subagent_outcomes = await asyncio.gather(
            orchestrator_calls(), subagent_calls()
        )

        # The orchestrator (full scope) must never be blocked.
        assert len(orchestrator_results) == 10

        # The sub-agent (read-only scope) must be blocked on EVERY attempt
        # at delete_record, even while interleaved with the orchestrator's
        # successful calls to the exact same tool in the same process. If
        # the old discover-cache approach were still in use, some of these
        # would incorrectly succeed (cross-contaminated with the
        # orchestrator's cached identity).
        assert subagent_outcomes == ["blocked"] * 10

        ok, bad_id = verify_chain(conn)
        assert ok, f"hash chain broke at {bad_id}"

        blocked_events = conn.execute(
            "SELECT COUNT(*) FROM events WHERE agent_id = 'agent-subagent' AND event_type = 'tool.blocked'"
        ).fetchone()[0]
        assert blocked_events == 10

        allowed_events = conn.execute(
            "SELECT COUNT(*) FROM events WHERE agent_id = 'agent-orchestrator' AND result = 'success'"
        ).fetchone()[0]
        assert allowed_events == 10
    finally:
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
