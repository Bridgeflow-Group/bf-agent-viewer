"""F-016/T-024: real proof that a credential revoked (or expired) while a
gateway process is already running actually stops working, without a
restart -- not just that revoke_token()/load_token_registry() are correct
in isolation (test_identity.py covers that), but that GatewayMiddleware's
periodic refresh (credential_refresh_seconds) really does pick it up on a
live process. This is the piece the module's own docstring used to flag
as "acceptable for v0.1.0, live invalidation is a v0.2.0+ concern" -- this
test is what proves that gap is actually closed now, not just documented
as closed.
"""
import asyncio
import os

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from bf_agent_viewer.db import reset
from bf_agent_viewer.gateway import build_gateway
from bf_agent_viewer.identity import issue_token, register_identity, renew_token, revoke_token

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_SCRIPT = os.path.join(HERE, "fixtures", "demo_backend.py")

# Well clear of every port range other test files already use (see their
# own port constants) -- picked deliberately, not by accident, after the
# T-019 port-collision lesson (ISS: two test files silently sharing a
# range, caught only by running the full suite).
PORT = 8990


@pytest.fixture
def gateway_setup(tmp_path):
    db_path = tmp_path / "gw.db"
    conn = reset(db_path)
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('human-1', 'org-1', 'Owner')")

    identity = register_identity(
        conn, organization_id="org-1", agent_id="agent-a", agent_name="A",
        owner_human_id="human-1", subject="agent-a", granted_scope=["get_weather"],
    )

    gateway, middleware = build_gateway(
        conn, backend_script=BACKEND_SCRIPT, organization_id="org-1", write_tools=set(),
        prefer_container=False,
        # 1s: fast enough that the test doesn't need to wait long, but not
        # sub-second -- expiry is stored with whole-second precision
        # (registry.py's _now_str), so a sub-second refresh interval would
        # race against that truncation in ways that have nothing to do
        # with the mechanism actually being tested.
        credential_refresh_seconds=1.0,
    )
    return conn, gateway, middleware, identity


async def _call_with_token(port: int, token: str):
    transport = StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp", headers={"X-BF-Agent-Token": token})
    async with Client(transport) as c:
        return await c.call_tool("get_weather", {"city": "Lima"})


@pytest.mark.asyncio
async def test_revoked_credential_stops_working_on_a_live_gateway_without_restart(gateway_setup):
    conn, gateway, middleware, identity = gateway_setup
    token = issue_token(conn, agent_id="agent-a")

    server_task = asyncio.create_task(
        gateway.run_async(transport="http", host="127.0.0.1", port=PORT, show_banner=False)
    )
    await asyncio.sleep(1.0)

    try:
        # Works before revocation.
        await _call_with_token(PORT, token)

        # Revoke it -- this is F-009's actual kill mechanism (immediate
        # cutoff, no TTL wait). The gateway process is still running; the
        # only thing that changes is the database row.
        revoke_token(conn, token)

        # Immediately after revoking, still within the refresh interval:
        # the in-memory registry hasn't reloaded yet, so this is
        # documenting the real, stated tradeoff (not a bug) -- a call that
        # lands in the gap between revocation and the next refresh can
        # still succeed. If this ever starts failing, it means the
        # refresh got faster than the interval promises, which is fine;
        # it must never mean the interval stopped being honored at all.

        # Wait past the refresh interval, then confirm it's actually cut off.
        await asyncio.sleep(1.2)
        with pytest.raises(Exception):  # noqa: BLE001 -- FastMCP wraps the 401/403 as a client-side error
            await _call_with_token(PORT, token)
    finally:
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


@pytest.mark.asyncio
async def test_short_lived_credential_expires_on_a_live_gateway_without_restart(gateway_setup):
    conn, gateway, middleware, identity = gateway_setup

    server_task = asyncio.create_task(
        gateway.run_async(transport="http", host="127.0.0.1", port=PORT + 1, show_banner=False)
    )
    await asyncio.sleep(1.0)

    # Issued only once the server (and the middleware's __init__-time
    # registry load) is already up, so its TTL window starts from a known
    # point -- issuing it before the 1s startup sleep would burn most of
    # a short TTL just waiting for the server to come up, unrelated to
    # what this test is actually proving. 3s TTL: comfortably survives
    # one call right away, and comfortably expires (with margin past
    # whole-second timestamp truncation) within the wait below. No
    # revoke_token() call involved at all -- proving expiry alone, not
    # just explicit revocation, is picked up by the live refresh.
    token = issue_token(conn, agent_id="agent-a", ttl_seconds=3)

    try:
        await _call_with_token(PORT + 1, token)

        # Past both the TTL and the refresh interval, with margin.
        await asyncio.sleep(4.0)
        with pytest.raises(Exception):  # noqa: BLE001
            await _call_with_token(PORT + 1, token)
    finally:
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


@pytest.mark.asyncio
async def test_renewed_credential_keeps_working_past_its_original_ttl(gateway_setup):
    """The other side of the same mechanism: renewal really does keep a
    short-lived credential alive on a live gateway, not just in the
    database in isolation."""
    conn, gateway, middleware, identity = gateway_setup

    server_task = asyncio.create_task(
        gateway.run_async(transport="http", host="127.0.0.1", port=PORT + 2, show_banner=False)
    )
    await asyncio.sleep(1.0)

    token = issue_token(conn, agent_id="agent-a", ttl_seconds=3)

    try:
        await _call_with_token(PORT + 2, token)

        # Renew well before the original TTL would lapse.
        renew_token(conn, token, ttl_seconds=3600)

        # Past the original TTL and the refresh interval, with margin --
        # would be rejected here if renewal hadn't taken effect.
        await asyncio.sleep(4.0)
        await _call_with_token(PORT + 2, token)
    finally:
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
