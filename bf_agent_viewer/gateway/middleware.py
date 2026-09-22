"""The gateway's real middleware: identity resolution, scope enforcement,
per-agent rate limiting, and tamper-evident event logging, wired together
into the one FastMCP middleware hook that fires on every tools/call.

Identity resolution note (IMPORTANT, found Sept 22 2026 while building this):
the prototype's validated approach -- cache clientInfo from the one-time
discover/initialize negotiation, reuse it for every later tools/call on
"the connection" -- only ever worked because the prototype spawned one OS
process per connection (stdio). Tested directly against a real persistent
multi-connection gateway process (the actual production architecture) and
confirmed empirically: MCP's stateless design gives a shared process
NOTHING connection-scoped to key a cache on. fctx.session_id, request_id,
and even Context/session object identity all change on every single
request -- not just across connections, on every request within the same
connection. clientInfo itself is never resent after discover. So this
module does NOT use that caching approach. Instead, identity is resolved
fresh on every request from a bearer token presented in a request header
(X-BF-Agent-Token) -- checked as a dict lookup against a token registry
loaded at process startup, no per-request DB read and no connection state
required. clientInfo is still captured where available and logged as
descriptive metadata (what the client claims about itself), but it is
never the trust boundary -- consistent with this project's own existing
principle (see research.md) that a self-reported client name never is.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from bf_agent_viewer.alerts import AlertChannel, LoggingAlertChannel, fire_alert
from bf_agent_viewer.events import last_hash, log_event
from bf_agent_viewer.identity import Identity, load_token_registry
from bf_agent_viewer.ratelimit import TokenBucketLimiter

logger = logging.getLogger("bf_agent_viewer.gateway.middleware")

FALLBACK_AGENT_ID = "agent-unregistered"
TOKEN_HEADER = "x-bf-agent-token"


def _request_headers(context: MiddlewareContext) -> dict[str, str] | None:
    """Best-effort: only present for HTTP-transport connections. stdio
    connections have no HTTP request to read a header from -- for those,
    a v0.1.0 deployment is inherently single-agent-per-process anyway
    (each stdio connection is its own OS process), so there is no identity
    ambiguity to resolve there in the first place."""
    try:
        fctx = context.fastmcp_context
        return dict(fctx.request_context.request.headers)
    except Exception:  # noqa: BLE001
        return None


def _client_info(context: MiddlewareContext) -> Any:
    try:
        import mcp_types as mt
        meta = getattr(context.message.params, "meta", None) if hasattr(context.message, "params") else None
        if meta:
            return meta.get(mt.CLIENT_INFO_META_KEY) if isinstance(meta, dict) else meta
    except Exception:  # noqa: BLE001
        pass
    return None


class GatewayMiddleware(Middleware):
    """One instance per gateway process, serving concurrently many real
    connections (the persistent-HTTP architecture validated Sept 22 2026:
    99.5 writes/sec aggregate, 15 concurrent real agents, zero loss)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        organization_id: str,
        write_tools: set[str],
        rate_limiter: TokenBucketLimiter | None = None,
        alert_channel: AlertChannel | None = None,
    ):
        self.conn = conn
        self.organization_id = organization_id
        self.write_tools = write_tools
        self.rate_limiter = rate_limiter or TokenBucketLimiter()
        # F-036: defaults to LoggingAlertChannel, never a no-op -- an
        # unconfigured deployment still sees rate-limit rejections in its
        # own logs, rather than the alert silently going nowhere.
        self.alert_channel = alert_channel or LoggingAlertChannel()

        self.running_hash = last_hash(conn)
        self.token_registry = load_token_registry(conn)

    def _resolve(self, context: MiddlewareContext) -> tuple[Identity | None, str | None]:
        headers = _request_headers(context)
        token = headers.get(TOKEN_HEADER) if headers else None
        identity = self.token_registry.get(token) if token else None
        return identity, token

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        tool_name = context.message.name
        args = context.message.arguments or {}
        identity, token = self._resolve(context)
        client_info = _client_info(context)

        agent_id = identity.agent_id if identity else FALLBACK_AGENT_ID
        granted_scope = identity.granted_scope if identity else None
        action = "write" if tool_name in self.write_tools else "read"

        # Rate limiting (F-041) -- checked before the scope check so a
        # rate-limited agent doesn't also get a misleading "out of scope"
        # denial for a call that was never going to run anyway.
        if not self.rate_limiter.allow(agent_id):
            _, self.running_hash = log_event(
                self.conn, organization_id=self.organization_id, agent_id=agent_id,
                session_id=None, actor_human_id=None, event_type="tool.rate_limited",
                action=action, tool_id=tool_name, result="denied",
                metadata={"arguments": args, "reason": "rate limit exceeded"},
                prev_hash=self.running_hash,
            )
            self.conn.commit()
            # security.md's own stated promise: "a rejection itself is
            # surfaced as an alert" (F-036/F-041). fire_alert() persists
            # regardless of delivery and never raises on a delivery
            # failure, so this can't turn a rate-limit rejection into an
            # unrelated 500 if the configured channel is unreachable.
            fire_alert(
                self.conn, self.alert_channel, organization_id=self.organization_id,
                agent_id=agent_id, alert_type="rate_limited", severity="warning",
                message=f"agent '{agent_id}' exceeded its rate limit calling '{tool_name}'",
                metadata={"tool": tool_name},
            )
            raise PermissionError(f"agent '{agent_id}' exceeded its rate limit")

        # Scope enforcement (OQ-003): a registered identity's granted_scope
        # is a hard allowlist. An unregistered/unresolved connection
        # (granted_scope is None) is NOT blocked here -- v0.1.0 is
        # visibility-first (see security.md): unclaimed agents are
        # visible, not silently dropped. Enforcement-by-default for
        # unregistered callers is a posture change for later, not this
        # module's call to make alone.
        if granted_scope is not None and tool_name not in granted_scope:
            _, self.running_hash = log_event(
                self.conn, organization_id=self.organization_id, agent_id=agent_id,
                session_id=None, actor_human_id=None, event_type="tool.blocked",
                action=action, tool_id=tool_name, result="denied",
                metadata={
                    "arguments": args,
                    "reason": "tool not in identity's granted_scope",
                    "granted_scope": sorted(granted_scope),
                    "identity_id": identity.identity_id,
                    "parent_identity_id": identity.parent_identity_id,
                    "client_info_asserted": str(client_info) if client_info else None,
                },
                prev_hash=self.running_hash,
            )
            self.conn.commit()
            raise PermissionError(
                f"agent identity '{agent_id}' is not permitted to call '{tool_name}' "
                f"(granted_scope={sorted(granted_scope)})"
            )

        t0 = time.perf_counter()
        error = None
        try:
            result = await call_next(context)
            outcome = "success"
        except Exception as e:  # noqa: BLE001
            outcome = "error"
            error = str(e)
            raise
        finally:
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            _, self.running_hash = log_event(
                self.conn, organization_id=self.organization_id, agent_id=agent_id,
                session_id=None, actor_human_id=None, event_type="tool.called",
                action=action, tool_id=tool_name,
                metadata={
                    "arguments": args,
                    "latency_ms": latency_ms,
                    "client_info_asserted": str(client_info) if client_info else None,
                    "identity_id": identity.identity_id if identity else None,
                    "parent_identity_id": identity.parent_identity_id if identity else None,
                    "error": error,
                },
                prev_hash=self.running_hash,
            )
            self.conn.commit()
        return result
