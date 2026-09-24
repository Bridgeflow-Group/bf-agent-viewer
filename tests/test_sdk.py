"""F-024/T-019: bf_agent_viewer.sdk in isolation -- payload shape and
error-handling semantics, mocked at the HTTP layer (test_otel_ingest.py
covers the real client-against-real-gateway path)."""
import json
from unittest.mock import MagicMock, patch

import pytest

from bf_agent_viewer.sdk import BFAgentViewerClient, BFAgentViewerReportError


def _client(**kwargs):
    return BFAgentViewerClient(gateway_url="http://localhost:8941", token="bfav_test", **kwargs)


def test_record_tool_call_posts_genai_shaped_attributes():
    client = _client(agent_name="my-agent", raise_on_error=True)
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = json.loads(request.data)
        cm = MagicMock()
        cm.__enter__.return_value = MagicMock(read=lambda: b"{}")
        cm.__exit__.return_value = False
        return cm

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.record_tool_call(
            "send_email", arguments={"to": "x@example.com"}, result="success",
            duration_ms=12.5, tool_call_id="call-1",
        )

    assert captured["url"] == "http://localhost:8941/v1/otel/events"
    assert captured["headers"]["x-bf-agent-token"] == "bfav_test"
    event = captured["body"]["events"][0]
    attrs = event["attributes"]
    assert attrs["gen_ai.operation.name"] == "execute_tool"
    assert attrs["gen_ai.tool.name"] == "send_email"
    assert attrs["gen_ai.tool.call.id"] == "call-1"
    assert attrs["gen_ai.tool.call.arguments"] == {"to": "x@example.com"}
    assert attrs["gen_ai.agent.name"] == "my-agent"
    assert attrs["error.type"] is None
    assert event["status"] == "OK"
    assert event["duration_ms"] == 12.5


def test_record_tool_call_marks_status_error_on_error_type():
    client = _client(raise_on_error=True)
    captured = {}

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        cm = MagicMock()
        cm.__enter__.return_value = MagicMock(read=lambda: b"{}")
        cm.__exit__.return_value = False
        return cm

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.record_tool_call("send_email", result="error", error_type="TimeoutError")

    event = captured["body"]["events"][0]
    assert event["status"] == "ERROR"
    assert event["attributes"]["error.type"] == "TimeoutError"


def test_trace_tool_call_reports_success_and_returns_normally():
    client = _client(raise_on_error=True)
    reported = []

    with patch.object(client, "record_tool_call", side_effect=lambda *a, **k: reported.append(k)):
        with client.trace_tool_call("send_email", arguments={"to": "x"}):
            pass

    assert reported[0]["result"] == "success"
    assert reported[0]["error_type"] is None
    assert reported[0]["duration_ms"] >= 0


def test_trace_tool_call_reports_error_but_still_reraises_original_exception():
    client = _client(raise_on_error=True)
    reported = []

    with patch.object(client, "record_tool_call", side_effect=lambda *a, **k: reported.append(k)):
        with pytest.raises(ValueError, match="boom"):
            with client.trace_tool_call("send_email"):
                raise ValueError("boom")

    assert reported[0]["result"] == "error"
    assert reported[0]["error_type"] == "ValueError"


def test_trace_tool_call_never_masks_the_original_exception_with_a_report_failure():
    """If reporting itself fails (gateway unreachable) while the wrapped
    block also raised, the wrapped block's own exception must win -- a
    reporting problem is never allowed to hide the real error."""
    client = _client(raise_on_error=True)

    def failing_record(*args, **kwargs):
        raise BFAgentViewerReportError("gateway unreachable")

    with patch.object(client, "record_tool_call", side_effect=failing_record):
        with pytest.raises(ValueError, match="boom"):
            with client.trace_tool_call("send_email"):
                raise ValueError("boom")


def test_reporting_failure_does_not_raise_by_default():
    """raise_on_error defaults to False -- an agent's own work should not
    fail just because this platform's gateway happened to be unreachable
    when it went to report what already happened."""
    client = _client()  # raise_on_error=False (default)

    import urllib.error

    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("connection refused")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.record_tool_call("send_email", result="success")  # must not raise


def test_reporting_failure_raises_when_opted_in():
    client = _client(raise_on_error=True)

    import urllib.error

    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("connection refused")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(BFAgentViewerReportError):
            client.record_tool_call("send_email", result="success")
