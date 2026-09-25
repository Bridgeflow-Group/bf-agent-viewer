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
from bf_agent_viewer.identity import Identity, discover_agent, load_token_registry
from bf_agent_viewer.ratelimit import TokenBucketLimiter

logger = logging.getLogger("bf_agent_viewer.gateway.middleware")

TOKEN_HEADER = "x-bf-agent-token"

# F-047: the reserved tool name an agent calls to send a liveness signal.
# A native tool registered directly on the gateway's FastMCP instance
# (see gateway/server.py), not proxied to the backend -- so it works even
# when the backend itself is slow, idle, or has nothing to do right now.
# Reserved: a backend that happens to define its own tool with this exact
# name will collide with it; documented in CLI.md rather than solved with
# collision detection, matching how this project states known constraints
# plainly instead of engineering around every edge case up front.
HEARTBEAT_TOOL_NAME = "bf_heartbeat"

# F-016/T-024: how often a live gateway process re-loads its active-token
# registry from the database, in seconds. Well under the sub-5-minute
# kill-switch target (OQ-004) with real headroom -- a revoked or
# unrenewed-and-expired credential is picked up within one interval of
# real time on an agent that's still actively calling, not just at the
# next process restart. Configurable (--credential-refresh-seconds /
# BF_CREDENTIAL_REFRESH_SECONDS, see cli.py) since the right tradeoff
# between "how fast can a kill take effect" and "one extra DB query per
# interval" depends on deployment scale, not something to hardcode.
DEFAULT_CREDENTIAL_REFRESH_SECONDS = 30.0


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
        credential_refresh_seconds: float = DEFAULT_CREDENTIAL_REFRESH_SECONDS,
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
        # T-014 (org isolation audit): filtered to this gateway's own
        # organization_id -- previously loaded every organization's active
        # tokens into one shared dict, so a bearer token issued for a
        # different organization's agent would authenticate successfully
        # here too, as long as both shared the same database file.
        self.token_registry = load_token_registry(conn, organization_id=organization_id)
        # F-016/T-024: when this was loaded, in monotonic time (never
        # wall-clock -- immune to a system clock adjustment). Paired with
        # credential_refresh_seconds in _maybe_refresh_token_registry()
        # below, this is what makes a short-lived credential's expiry, or
        # an explicit revoke_token() call, actually take effect on a live
        # gateway process instead of only at the next restart -- the gap
        # this module's own docstring used to flag as "acceptable for
        # v0.1.0, live invalidation is a v0.2.0+ concern." v0.2.0 is now.
        self._token_registry_loaded_at = time.monotonic()
        self.credential_refresh_seconds = credential_refresh_seconds

    def _maybe_refresh_token_registry(self) -> None:
        """Reload self.token_registry from the database if more than
        credential_refresh_seconds have passed since the last load.
        Checked synchronously at the top of every identity resolution
        (both _resolve, for the MCP path, and resolve_token, for the
        OTel SDK ingestion path -- F-024/T-019) rather than run as a
        separate background task: a plain timestamp comparison is cheap
        enough to pay on every request, and doing it this way means no
        new concurrency to reason about -- it runs on the same thread,
        at the same point in the request lifecycle, as everything else
        this module already does before touching running_hash (see the
        module docstring's concurrency-safety note). The tradeoff worth
        being explicit about: a revoked or expired credential is only
        guaranteed gone from self.token_registry as of the *next* request
        this method happens to run on after the interval elapses, not on
        a wall-clock timer -- for an agent that's actively calling
        (the realistic case for something you'd want to kill), that's
        within one interval of real time; for one that's already gone
        quiet, there's nothing left to block anyway.
        """
        now = time.monotonic()
        if now - self._token_registry_loaded_at >= self.credential_refresh_seconds:
            self.token_registry = load_token_registry(self.conn, organization_id=self.organization_id)
            self._token_registry_loaded_at = now

    def _fire_discovery_alert(self, agent_id: str, client_info: Any) -> None:
        """F-048/T-039: fire an F-036 alert the moment discover_agent()
        reports first_sighting=True, right alongside the one-time
        agent.discovered event both call sites below already log --
        same wiring pattern gateway/resilience.py's gap-marker alert
        already uses (persist-then-deliver, never raises on a delivery
        failure). Non-blocking on purpose: F-027 passive discovery
        already makes a newly-seen agent visible on the dashboard with
        or without this: the alert is what means someone doesn't have
        to be staring at the dashboard right when it happens to notice
        -- so severity="info", not "warning", since an unclaimed agent
        showing up isn't itself a problem, just something worth a look."""
        fire_alert(
            self.conn, self.alert_channel, organization_id=self.organization_id,
            agent_id=agent_id, alert_type="agent_discovered", severity="info",
            message=f"a new, unclaimed agent ('{agent_id}') was passively discovered -- "
                    f"claim it with 'bf-agent-viewer claim' to assign an owner and issue it a real credential",
            metadata={"client_info_asserted": str(client_info) if client_info else None},
        )

    def _resolve(self, context: MiddlewareContext) -> tuple[Identity | None, str | None]:
        self._maybe_refresh_token_registry()
        headers = _request_headers(context)
        token = headers.get(TOKEN_HEADER) if headers else None
        identity = self.token_registry.get(token) if token else None
        return identity, token

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        tool_name = context.message.name
        args = context.message.arguments or {}
        identity, token = self._resolve(context)
        client_info = _client_info(context)

        if identity:
            agent_id = identity.agent_id
        elif token is not None:
            # F-016/T-024: a token WAS presented but doesn't resolve --
            # revoked, expired, or simply bogus. Deliberately NOT the same
            # as no token at all: falling through to passive discovery
            # here (as an earlier version of this method did) meant
            # revoking a credential actually *increased* what an agent
            # could do, from its previously scoped granted_scope to
            # completely unscoped (passive discovery's granted_scope=None
            # means no restriction at all) -- the exact opposite of what
            # F-009's kill switch needs revoke_token()/an expired TTL to
            # mean. Rejected outright instead, and logged as its own event
            # type so a flood of these is visible on the dashboard (an
            # agent still trying to use a credential that's been cut off
            # is worth knowing about) rather than silently folded into
            # ordinary passive-discovery traffic.
            #
            # events.agent_id is NOT NULL, so this still needs a real
            # agents row to log against even though the credential isn't
            # usable: if the token matches a real (revoked/expired)
            # credentials row *belonging to this gateway's own
            # organization*, log it under the agent that credential
            # actually belonged to -- more accurate than a fresh
            # client_info-based discovery. The organization_id filter
            # here matters (T-014 discipline): without it, a token issued
            # for a DIFFERENT organization sharing this database file
            # would leak that organization's real agent_id into this
            # organization's event log -- an org-a event pointing at an
            # org-b agent row, which is exactly the cross-org leak T-014
            # closed elsewhere. A token that doesn't match any credential
            # in this organization (never issued at all, or issued for
            # another organization) falls back to discover_agent()
            # instead, the same visibility mechanism a no-token caller
            # gets -- it's still rejected either way, this only affects
            # which agent_id the rejection event is logged under.
            cred_row = self.conn.execute(
                """SELECT c.agent_id FROM credentials c
                   JOIN agents ag ON ag.id = c.agent_id
                   WHERE c.id = ? AND ag.organization_id = ?""",
                (token, self.organization_id),
            ).fetchone()
            if cred_row is not None:
                agent_id = cred_row[0]
            else:
                # Same first-sighting discovery event a no-token caller
                # gets (below) -- a bogus/never-issued/cross-org token is
                # still worth surfacing as a newly-seen caller on the
                # dashboard, same visibility principle, even though the
                # call itself is being rejected either way.
                agent_id, first_sighting = discover_agent(
                    self.conn, organization_id=self.organization_id, client_info=client_info,
                )
                if first_sighting:
                    _, self.running_hash = log_event(
                        self.conn, organization_id=self.organization_id, agent_id=agent_id,
                        session_id=None, actor_human_id=None, event_type="agent.discovered",
                        action=None, tool_id=None, result="success",
                        metadata={"client_info_asserted": str(client_info) if client_info else None},
                        prev_hash=self.running_hash,
                    )
                    self.conn.commit()
                    self._fire_discovery_alert(agent_id, client_info)
            _, self.running_hash = log_event(
                self.conn, organization_id=self.organization_id, agent_id=agent_id,
                session_id=None, actor_human_id=None, event_type="tool.rejected_unrecognized_token",
                action="write" if tool_name in self.write_tools else "read", tool_id=tool_name,
                result="denied",
                metadata={
                    "arguments": args,
                    "reason": "presented token does not resolve to an active credential "
                              "(revoked, expired, or invalid)",
                    "client_info_asserted": str(client_info) if client_info else None,
                },
                prev_hash=self.running_hash,
            )
            self.conn.commit()
            raise PermissionError(
                "the presented credential is not active (revoked, expired, or invalid) -- "
                "register a new one or have this one renewed"
            )
        else:
            # T-012 (F-027, passive discovery): an unrecognized caller no
            # longer just gets its traffic stamped with a fixed sentinel
            # id that nothing else in the product can see -- it gets a
            # real, visible-but-unowned `agents` row, so it shows up on
            # the dashboard the moment it's first seen. Still not trusted
            # (granted_scope stays None below, same as before) -- this is
            # about visibility, not authorization. See identity/discovery.py.
            # Only reached when NO token was presented at all -- a
            # presented-but-unrecognized token is rejected above instead
            # (F-016/T-024), not treated the same as never having one.
            agent_id, first_sighting = discover_agent(
                self.conn, organization_id=self.organization_id, client_info=client_info,
            )
            if first_sighting:
                _, self.running_hash = log_event(
                    self.conn, organization_id=self.organization_id, agent_id=agent_id,
                    session_id=None, actor_human_id=None, event_type="agent.discovered",
                    action=None, tool_id=None, result="success",
                    metadata={"client_info_asserted": str(client_info) if client_info else None},
                    prev_hash=self.running_hash,
                )
                self.conn.commit()
                self._fire_discovery_alert(agent_id, client_info)

        # F-047: a heartbeat isn't a real capability -- it doesn't touch
        # the backend, doesn't need to be in anyone's granted_scope, and
        # shouldn't compete with real work for the same rate-limit budget
        # (the agent should never get rate-limited out of saying "I'm
        # still alive," least of all when it's busy). Handled entirely
        # outside the scope/rate-limit checks below.
        if tool_name == HEARTBEAT_TOOL_NAME:
            return await self._handle_heartbeat(context, call_next, agent_id)

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

    def resolve_token(self, token: str | None) -> Identity | None:
        """Same lookup `_resolve` does for an MCP connection's header, but
        callable directly from something that isn't a MiddlewareContext --
        the OTel SDK ingestion route (gateway/otel_ingest.py, F-024/T-019)
        reads its bearer token from a plain Starlette Request instead.
        Also refreshes the token registry on the same schedule as the MCP
        path (F-016/T-024) -- a revoked or expired credential must lapse
        on this path too, not just the one _resolve covers."""
        self._maybe_refresh_token_registry()
        return self.token_registry.get(token) if token else None

    def log_otel_event(
        self,
        *,
        agent_id: str,
        tool_id: str | None,
        action: str | None,
        result: str,
        metadata: dict[str, Any],
        event_type: str = "tool.called",
    ) -> str:
        """Write one event reported through the OTel SDK ingestion path
        (F-024/T-019) into the same tamper-evident chain the MCP gateway
        path writes to -- one running_hash, one writer, regardless of
        which instrumentation path produced the event, so the chain
        stays a single coherent history rather than forking per path.

        Deliberately NOT async and does no I/O of its own between reading
        self.running_hash and reassigning it (matches on_call_tool's own
        discipline, see that method's docstring reasoning) -- this is
        what keeps concurrent calls into this method safe on a single
        asyncio event loop without needing an explicit lock: nothing here
        yields control between the read and the write. The caller (the
        route handler in otel_ingest.py) is responsible for doing its own
        I/O (reading the request body, resolving the token) *before*
        calling this, not after.

        Always logged as event_type="tool.called", source="otel_sdk" --
        unlike the MCP gateway path, this method never blocks or rejects
        the underlying tool call: by the time this is invoked, the real
        call already happened out-of-band (the SDK reports after the
        fact, it doesn't proxy). A scope violation or rate-limit signal
        from this path is recorded as an alert-worthy observation, never
        an enforcement action -- see otel_ingest.py for that logic.
        """
        event_id, self.running_hash = log_event(
            self.conn, organization_id=self.organization_id, agent_id=agent_id,
            session_id=None, actor_human_id=None, event_type=event_type,
            action=action, tool_id=tool_id, result=result, source="otel_sdk",
            metadata=metadata, prev_hash=self.running_hash,
        )
        self.conn.commit()
        return event_id

    async def _handle_heartbeat(self, context: MiddlewareContext, call_next: CallNext, agent_id: str):
        """F-047: logs a distinct agent.heartbeat event (not tool.called,
        so it reads clearly in the activity history as a liveness pulse
        rather than real work) -- but it's still a real event with a real
        occurred_at, so it counts toward F-046's online/offline derivation
        the same as any other activity. This is what actually closes the
        gap F-046 alone leaves open: an agent that's alive but genuinely
        idle (no tool calls to make right now) looks identical to a dead
        one under F-046's activity-only view -- a heartbeat lets it keep
        showing "online" without needing to fabricate real tool calls just
        to stay visible."""
        result = await call_next(context)
        _, self.running_hash = log_event(
            self.conn, organization_id=self.organization_id, agent_id=agent_id,
            session_id=None, actor_human_id=None, event_type="agent.heartbeat",
            action=None, tool_id=HEARTBEAT_TOOL_NAME, result="success", metadata={},
            prev_hash=self.running_hash,
        )
        self.conn.commit()
        return result
