"""Assembles and runs the real gateway: a persistent process (the
architecture validated Sept 22 2026 -- one process, many concurrent real
connections, not the prototype's one-process-per-connection scaffolding)
proxying to a backend MCP server with a sandboxed spawn."""
from __future__ import annotations

import sqlite3

from fastmcp import FastMCP
from fastmcp.client.transports import StdioTransport
from fastmcp.server.providers.proxy import ProxyClient, ProxyProvider

from bf_agent_viewer.alerts import AlertChannel
from bf_agent_viewer.gateway.middleware import HEARTBEAT_TOOL_NAME, GatewayMiddleware
from bf_agent_viewer.gateway.otel_ingest import register_otel_ingest_route
from bf_agent_viewer.gateway.resilience import DEFAULT_GAP_THRESHOLD_SECONDS, check_and_log_gap
from bf_agent_viewer.gateway.sandbox import (
    ContainerPolicy,
    SandboxPolicy,
    build_backend_stdio_args,
)
from bf_agent_viewer.ratelimit import TokenBucketLimiter


def build_gateway(
    conn: sqlite3.Connection,
    *,
    backend_script: str,
    organization_id: str,
    write_tools: set[str],
    name: str = "bf-agent-viewer-gateway",
    rate_limiter: TokenBucketLimiter | None = None,
    alert_channel: AlertChannel | None = None,
    sandbox_policy: SandboxPolicy | None = None,
    container_policy: ContainerPolicy | None = None,
    prefer_container: bool = True,
    gap_threshold_seconds: float = DEFAULT_GAP_THRESHOLD_SECONDS,
) -> tuple[FastMCP, GatewayMiddleware]:
    policy = sandbox_policy or SandboxPolicy()

    # F-042: run before GatewayMiddleware(...) below picks up last_hash(conn)
    # for itself, so a logged gap event becomes part of the chain the
    # middleware sees, not a fork off to the side.
    check_and_log_gap(
        conn,
        organization_id=organization_id,
        alert_channel=alert_channel,
        gap_threshold_seconds=gap_threshold_seconds,
    )

    # build_backend_stdio_args (ISS-017/F-044) runs the backend inside a
    # locked-down container (real filesystem/network isolation) when
    # Docker is reachable, falling back to the rlimit-bootstrap-only path
    # otherwise -- see gateway/sandbox.py for both mechanisms and why
    # neither one is a stand-in for the other.
    command, args, env = build_backend_stdio_args(
        backend_script, policy, container_policy, prefer_container=prefer_container,
    )

    # Single persistent backend client, reused across every call (the
    # ~1.4s/call connection-per-call bug fixed and validated in the
    # prototype, ISS-004) -- fastmcp's Client is reentrant/ref-counted, so
    # one instance from the factory keeps one persistent backend
    # connection alive.
    backend_transport = StdioTransport(command=command, args=args, env=env, cwd=policy.cwd)
    backend_client = ProxyClient(backend_transport)

    def client_factory():
        return backend_client

    gateway = FastMCP(name)
    gateway.add_provider(ProxyProvider(client_factory))

    # F-047: a native tool, not proxied to the backend -- so it works even
    # when the backend is slow, idle, or unreachable. Resolved via OQ-026:
    # a real inbound "ping the agent" isn't possible against MCP's
    # stateless per-request model (OQ-015) or agents that only ever
    # connect outward, so this flips the direction -- the agent calls
    # this on its own interval to signal it's alive, same auth path as
    # any other tool call. GatewayMiddleware special-cases this tool name
    # to skip scope/rate-limit checks and log it as its own event type
    # (see middleware.py's _handle_heartbeat).
    @gateway.tool(
        name=HEARTBEAT_TOOL_NAME,
        description="Liveness signal (F-047). Call on an interval to confirm this agent is still alive, independent of any real tool call.",
    )
    def _heartbeat() -> dict:
        return {"status": "ok"}

    middleware = GatewayMiddleware(
        conn,
        organization_id=organization_id,
        write_tools=write_tools,
        rate_limiter=rate_limiter,
        alert_channel=alert_channel,
    )
    gateway.add_middleware(middleware)

    # F-024/T-019: the OTel-SDK fallback ingestion route, for agents that
    # never connect through the MCP proxy above at all. Registered after
    # `middleware` exists -- the route reads identity/rate-limit/alert
    # state from it directly (see otel_ingest.py) rather than duplicating
    # any of that setup.
    register_otel_ingest_route(gateway, middleware)

    return gateway, middleware
