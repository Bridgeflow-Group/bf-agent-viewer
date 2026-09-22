"""Assembles and runs the real gateway: a persistent process (the
architecture validated Sept 22 2026 -- one process, many concurrent real
connections, not the prototype's one-process-per-connection scaffolding)
proxying to a backend MCP server with a sandboxed spawn."""
from __future__ import annotations

import sqlite3

from fastmcp import FastMCP
from fastmcp.client.transports import PythonStdioTransport
from fastmcp.server.providers.proxy import ProxyClient, ProxyProvider

from bf_agent_viewer.gateway.middleware import GatewayMiddleware
from bf_agent_viewer.gateway.sandbox import SandboxPolicy, build_env
from bf_agent_viewer.ratelimit import TokenBucketLimiter


def build_gateway(
    conn: sqlite3.Connection,
    *,
    backend_script: str,
    organization_id: str,
    write_tools: set[str],
    name: str = "bf-agent-viewer-gateway",
    rate_limiter: TokenBucketLimiter | None = None,
    sandbox_policy: SandboxPolicy | None = None,
) -> tuple[FastMCP, GatewayMiddleware]:
    policy = sandbox_policy or SandboxPolicy()
    env = build_env(policy)

    # Single persistent backend client, reused across every call (the
    # ~1.4s/call connection-per-call bug fixed and validated in the
    # prototype, ISS-004) -- fastmcp's Client is reentrant/ref-counted, so
    # one instance from the factory keeps one persistent backend
    # connection alive.
    backend_transport = PythonStdioTransport(backend_script, env=env, cwd=policy.cwd)
    backend_client = ProxyClient(backend_transport)

    def client_factory():
        return backend_client

    gateway = FastMCP(name)
    gateway.add_provider(ProxyProvider(client_factory))
    middleware = GatewayMiddleware(
        conn,
        organization_id=organization_id,
        write_tools=write_tools,
        rate_limiter=rate_limiter,
    )
    gateway.add_middleware(middleware)
    return gateway, middleware
