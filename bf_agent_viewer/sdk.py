"""F-024/T-019: the OpenTelemetry-conventions SDK -- the fallback
instrumentation path for an agent that never goes through the MCP gateway
proxy (F-023) at all, e.g. one calling third-party REST APIs directly
with no MCP layer in between. See how-it-works.md section 3: this is
explicitly the lower-fidelity path (it only sees what an agent chooses
to report, after the fact, not every call as it happens) that trades
guaranteed interception for broader reach -- it works with any agent
code, not just one already speaking MCP.

Deliberately dependency-free: stdlib `urllib.request` only, matching
this project's existing discipline (see pyproject.toml) of not pulling
in a dependency an install doesn't need. An agent that already has
`requests` or `httpx` installed doesn't need either one just to report
an event here -- and a "lightweight SDK" that itself drags in a heavy
HTTP stack would be a contradiction in terms.

Talks to the gateway's own OTel ingestion route
(gateway/otel_ingest.py, POST /v1/otel/events) using the same
X-BF-Agent-Token bearer credential the MCP path uses -- one identity
system, two ways in. Get a token the same way an MCP-connected agent
would: `bf-agent-viewer register` (or, for a passively-discovered agent,
`bf-agent-viewer claim`) prints one.

Usage:

    from bf_agent_viewer.sdk import BFAgentViewerClient

    client = BFAgentViewerClient(
        gateway_url="http://localhost:8941",
        token="bfav_...",
        agent_name="billing-agent",
    )

    # Automatic timing + success/error reporting:
    with client.trace_tool_call("send_invoice", arguments={"customer": "acme"}):
        send_invoice(customer="acme")

    # Or report a call that already finished:
    client.record_tool_call(
        "send_invoice", arguments={"customer": "acme"},
        result="success", duration_ms=142.3,
    )
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

logger = logging.getLogger("bf_agent_viewer.sdk")

# Kept in sync with gateway/otel_ingest.py's own constants deliberately
# (not imported from there) -- this module has to work standalone,
# installed and imported by code running in a completely separate
# process/environment from the gateway it's talking to, often without
# the rest of this package's server-side modules (or their own
# dependencies) present at all.
OTEL_INGEST_PATH = "/v1/otel/events"
TOKEN_HEADER = "X-BF-Agent-Token"


class BFAgentViewerReportError(RuntimeError):
    """The gateway rejected an event report, or couldn't be reached at
    all. Deliberately a distinct, catchable type: a reporting failure
    should never be indistinguishable from a real error in the agent's
    own tool call, and (see trace_tool_call) should never be allowed to
    mask or replace one either."""


@dataclass
class BFAgentViewerClient:
    gateway_url: str
    token: str
    # Purely descriptive -- the same caveat as MCP's self-asserted
    # clientInfo.name applies here: this is a label used for passive
    # discovery when this token isn't (yet) a registered identity, never
    # a trust boundary. See gateway/otel_ingest.py.
    agent_name: str | None = None
    timeout: float = 5.0
    # A reporting failure defaults to *not* raising -- an agent's own
    # work should not fail just because this platform's gateway happened
    # to be unreachable when it went to report what already happened.
    # Set True for tests, or for a deployment that specifically wants to
    # know the moment reporting breaks.
    raise_on_error: bool = False

    def record_tool_call(
        self,
        tool_name: str,
        *,
        arguments: dict[str, Any] | None = None,
        result: str = "success",
        error_type: str | None = None,
        duration_ms: float | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        """Reports one already-completed tool call, GenAI-semantic-
        convention-shaped (see gateway/otel_ingest.py for the exact
        attribute names this maps to)."""
        event = {
            "attributes": {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": tool_name,
                "gen_ai.tool.call.id": tool_call_id,
                "gen_ai.tool.call.arguments": arguments or {},
                "gen_ai.agent.name": self.agent_name,
                "error.type": error_type,
            },
            "status": "ERROR" if (error_type or result == "error") else "OK",
            "duration_ms": duration_ms,
        }
        self._post({"events": [event]})

    @contextmanager
    def trace_tool_call(
        self,
        tool_name: str,
        *,
        arguments: dict[str, Any] | None = None,
        tool_call_id: str | None = None,
    ) -> Iterator[None]:
        """Times the wrapped block and reports it on exit -- success if
        it completes, the exception's type name as error_type if it
        raises. Re-raises the original exception either way; a reporting
        failure inside the `finally` below must never replace or mask
        whatever the wrapped block itself raised -- including when
        raise_on_error=True. (Found by test_sdk.py's own
        test_trace_tool_call_never_masks_the_original_exception_with_a_report_failure:
        an earlier version of this method re-raised the reporting
        failure unconditionally on raise_on_error, which during Python's
        own exception-unwind of the *original* error replaces it with
        the reporting error instead of chaining alongside it -- exactly
        the failure mode this method's own docstring says must not
        happen. Only surfaced when nothing else is already propagating.)
        """
        t0 = time.perf_counter()
        error_type: str | None = None
        block_raised = False
        try:
            yield
        except Exception as exc:
            error_type = type(exc).__name__
            block_raised = True
            raise
        finally:
            duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            try:
                self.record_tool_call(
                    tool_name, arguments=arguments, tool_call_id=tool_call_id,
                    result="error" if error_type else "success",
                    error_type=error_type, duration_ms=duration_ms,
                )
            except BFAgentViewerReportError:
                if self.raise_on_error and not block_raised:
                    raise

    def _post(self, payload: dict[str, Any]) -> None:
        url = f"{self.gateway_url.rstrip('/')}{OTEL_INGEST_PATH}"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", TOKEN_HEADER: self.token},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            error = BFAgentViewerReportError(
                f"gateway at {self.gateway_url!r} rejected the event report: HTTP {exc.code} {detail}"
            )
            if self.raise_on_error:
                raise error
            logger.warning(str(error))
        except urllib.error.URLError as exc:
            error = BFAgentViewerReportError(
                f"could not reach gateway at {self.gateway_url!r}: {exc.reason}"
            )
            if self.raise_on_error:
                raise error
            logger.warning(str(error))
