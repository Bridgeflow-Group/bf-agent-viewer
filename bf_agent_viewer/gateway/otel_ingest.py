"""F-024/T-019: the OpenTelemetry-conventions SDK's ingestion endpoint --
the fallback instrumentation path (per how-it-works.md section 3 and
OQ-002) for agents that never go through the MCP gateway proxy (F-023) at
all, e.g. an agent calling third-party REST APIs directly with no MCP
layer in between. F-023 sees every call because it sits in the request
path; this path only sees what an agent chooses to report, after the
fact -- lower fidelity by construction, not by an implementation gap. See
bf_agent_viewer/sdk.py for the client half of this pair.

Registered as a plain HTTP route on the gateway's own FastMCP instance
(FastMCP.custom_route -- "arbitrary HTTP endpoints outside the standard
MCP protocol", exactly what this is), not a separate process or port, so
a deployment doesn't need to stand up and secure a second service just to
accept fallback-path events. Same bearer-token header
(X-BF-Agent-Token) and the same token_registry the MCP path already
resolves identity from -- one identity system, two ways in.

Deliberately visibility-only, like the rest of v0.1.0: this endpoint
never blocks the underlying tool call, because by the time an event
reaches here the call already happened somewhere this platform was never
in the path for. A registered identity calling something outside its own
granted_scope, or a caller exceeding this endpoint's own rate limit, is
recorded and alerted on as a real signal worth someone's attention -- not
silently dropped -- but "recorded" is the ceiling; there is no gateway
proxy step here to actually deny it, unlike the MCP path.

Wire format is a deliberately small, honest subset of the OpenTelemetry
GenAI semantic conventions (gen_ai.* attribute names) rather than a
proprietary shape -- see docs/standards.md for why this project follows
external conventions over inventing its own where a real one exists.
Not a full OTLP/protobuf exporter (that's real added complexity a
"lightweight SDK" for teams without an existing OTel collector shouldn't
have to carry) -- a small JSON POST carrying the same attribute names, so
a team that *does* already run an OTel collector can still recognize the
shape immediately, and nothing here would need to change if a real OTLP
exporter path is added later.
"""
from __future__ import annotations

import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from bf_agent_viewer.alerts import fire_alert
from bf_agent_viewer.identity.discovery import discover_agent
from bf_agent_viewer.identity.registry import client_info_name

logger = logging.getLogger("bf_agent_viewer.gateway.otel_ingest")

OTEL_INGEST_PATH = "/v1/otel/events"
TOKEN_HEADER = "x-bf-agent-token"

# The GenAI semantic-convention attribute names this endpoint reads.
# Anything else in an event's "attributes" object is accepted and kept
# verbatim in the logged event's metadata (see _translate_event below)
# rather than rejected -- a caller on a newer/fuller version of the
# convention than this v0.1.0 subset targets shouldn't get a hard error
# for including attributes this endpoint doesn't specifically act on.
ATTR_OPERATION_NAME = "gen_ai.operation.name"
ATTR_TOOL_NAME = "gen_ai.tool.name"
ATTR_TOOL_CALL_ID = "gen_ai.tool.call.id"
ATTR_TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments"
ATTR_AGENT_NAME = "gen_ai.agent.name"
ATTR_AGENT_ID = "gen_ai.agent.id"
ATTR_ERROR_TYPE = "error.type"


class MalformedOtelEvent(ValueError):
    """A reported event didn't have enough shape to log anything useful
    from -- raised for a 400, never silently dropped or half-logged."""


def _parse_duration_ms(event: dict[str, Any]) -> float | None:
    """Prefers an explicit top-level duration_ms (the SDK's own timer,
    see sdk.py's trace_tool_call) over reconstructing it from
    start_time/end_time strings -- one less thing this endpoint has to
    parse and get timezone-handling right on, when the caller already
    knows the number."""
    if "duration_ms" in event and event["duration_ms"] is not None:
        try:
            return float(event["duration_ms"])
        except (TypeError, ValueError):
            return None
    start, end = event.get("start_time"), event.get("end_time")
    if not (isinstance(start, (int, float)) and isinstance(end, (int, float))):
        return None
    return round((end - start) * 1000, 2)


def _translate_event(event: dict[str, Any]) -> dict[str, Any]:
    """Turns one OTel-GenAI-shaped event dict into the pieces
    GatewayMiddleware.log_otel_event needs. Raises MalformedOtelEvent
    rather than guessing at defaults for a field this endpoint has no
    honest fallback for."""
    if not isinstance(event, dict):
        raise MalformedOtelEvent("each event must be a JSON object")
    attributes = event.get("attributes")
    if not isinstance(attributes, dict):
        raise MalformedOtelEvent("event missing an \"attributes\" object")

    tool_name = attributes.get(ATTR_TOOL_NAME)
    if not tool_name or not isinstance(tool_name, str):
        raise MalformedOtelEvent(
            f"event.attributes.{ATTR_TOOL_NAME!r} is required and must be a non-empty string "
            "-- this is the only field this endpoint treats as the tool being reported on"
        )

    error_type = attributes.get(ATTR_ERROR_TYPE)
    status = event.get("status")
    result = "error" if (error_type or status == "ERROR") else "success"

    duration_ms = _parse_duration_ms(event)

    metadata = {
        "arguments": attributes.get(ATTR_TOOL_CALL_ARGUMENTS) or {},
        "tool_call_id": attributes.get(ATTR_TOOL_CALL_ID),
        "operation_name": attributes.get(ATTR_OPERATION_NAME),
        "agent_name_asserted": attributes.get(ATTR_AGENT_NAME),
        "error_type": error_type,
        "duration_ms": duration_ms,
        # Kept verbatim -- see module docstring: a newer/fuller set of
        # gen_ai.* attributes than this endpoint specifically acts on is
        # preserved rather than discarded, so nothing a caller sent is
        # silently lost even if this version of the platform doesn't
        # do anything with it yet.
        "otel_attributes": attributes,
    }
    return {"tool_name": tool_name, "result": result, "metadata": metadata}


def register_otel_ingest_route(gateway, middleware) -> None:
    """Adds the F-024/T-019 ingestion route to an already-built gateway.
    Called from gateway/server.py's build_gateway, right after the
    GatewayMiddleware it needs is constructed."""

    @gateway.custom_route(OTEL_INGEST_PATH, methods=["POST"])
    async def ingest_otel_events(request: Request) -> JSONResponse:  # noqa: ANN001
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "request body must be valid JSON"}, status_code=400)

        events = body.get("events") if isinstance(body, dict) and "events" in body else [body]
        if not isinstance(events, list) or not events:
            return JSONResponse(
                {"error": "body must be a single event object, or {\"events\": [...]} with at least one event"},
                status_code=400,
            )

        try:
            translated = [_translate_event(e) for e in events]
        except MalformedOtelEvent as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        token = request.headers.get(TOKEN_HEADER)
        identity = middleware.resolve_token(token)

        if identity is not None:
            agent_id = identity.agent_id
            granted_scope = identity.granted_scope
        elif token is not None:
            # F-016/T-024: same fix as the MCP path (middleware.py's
            # on_call_tool) -- a token that WAS presented but doesn't
            # resolve (revoked, expired, bogus) must not be treated the
            # same as no token at all. Falling through to passive
            # discovery here wouldn't let a revoked identity actually
            # perform a call (this endpoint never could stop that, see
            # the module docstring), but it would still mean a
            # scope-violation report from a just-revoked identity gets
            # silently reclassified as unscoped (granted_scope=None
            # bypasses the violation check entirely) instead of rejected
            # -- the same category of gap, even though the stakes are
            # lower on this after-the-fact reporting path.
            return JSONResponse(
                {"error": "the presented credential is not active (revoked, expired, or invalid) -- "
                          "register a new one or have this one renewed"},
                status_code=401,
            )
        else:
            # Same passive-discovery path the MCP gateway falls back to
            # (identity/discovery.py) -- reused as-is by handing it a
            # client_info-shaped dict keyed on gen_ai.agent.name, the
            # closest OTel-side analog to MCP's self-asserted
            # clientInfo.name. Same caveat applies here as there: this is
            # a label for visibility, never a trust boundary.
            first_event_attrs = events[0].get("attributes", {}) if isinstance(events[0], dict) else {}
            asserted_name = first_event_attrs.get(ATTR_AGENT_NAME) or first_event_attrs.get(ATTR_AGENT_ID)
            client_info = {"name": asserted_name} if asserted_name else None
            agent_id, first_sighting = discover_agent(
                middleware.conn, organization_id=middleware.organization_id, client_info=client_info,
            )
            granted_scope = None
            if first_sighting:
                middleware.log_otel_event(
                    agent_id=agent_id, tool_id=None, action=None, result="success",
                    event_type="agent.discovered",
                    metadata={"client_info_asserted": client_info_name(client_info)},
                )

        # Ingestion-pipeline protection only (see module docstring): this
        # is NOT the same guarantee F-041's rate limiting gives the MCP
        # path. It can't stop the tool call this event describes -- that
        # already happened, out-of-band, before this request was ever
        # sent. It exists so a misbehaving or misconfigured reporter
        # can't flood this platform's own event log.
        if not middleware.rate_limiter.allow(agent_id):
            return JSONResponse(
                {"error": f"agent '{agent_id}' exceeded the OTel ingestion rate limit"},
                status_code=429,
            )

        logged_ids = []
        for item in translated:
            action = "write" if item["tool_name"] in middleware.write_tools else "read"
            metadata = item["metadata"]

            if granted_scope is not None and item["tool_name"] not in granted_scope:
                # Can't block it -- the call already happened somewhere
                # this platform was never in the path for. What this
                # *can* do honestly: log it plainly and raise it as an
                # alert, the same "surfaced, not silently dropped"
                # standard F-036 already holds the MCP path to.
                metadata = {**metadata, "scope_check": "outside granted_scope",
                            "granted_scope": sorted(granted_scope)}
                fire_alert(
                    middleware.conn, middleware.alert_channel,
                    organization_id=middleware.organization_id, agent_id=agent_id,
                    alert_type="otel_scope_violation_reported", severity="warning",
                    message=(
                        f"agent '{agent_id}' reported a tool call to '{item['tool_name']}' via the "
                        "OTel SDK outside its registered granted_scope -- visibility only, the "
                        "call already happened out-of-band and could not be blocked"
                    ),
                    metadata={"tool": item["tool_name"]},
                )

            event_id = middleware.log_otel_event(
                agent_id=agent_id, tool_id=item["tool_name"], action=action,
                result=item["result"], metadata=metadata,
            )
            logged_ids.append(event_id)

        return JSONResponse({"logged": logged_ids}, status_code=202)
