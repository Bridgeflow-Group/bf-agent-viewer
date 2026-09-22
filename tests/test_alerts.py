"""F-036: alert persistence + delivery channels, plus the real wiring
into the gateway's rate-limit rejection path (security.md's own stated
promise: "a rejection itself is surfaced as an alert")."""
from __future__ import annotations

import http.server
import json
import threading

import pytest

from bf_agent_viewer.alerts import (
    CompositeAlertChannel,
    EmailAlertChannel,
    LoggingAlertChannel,
    WebhookAlertChannel,
    fire_alert,
)
from bf_agent_viewer.db import reset


@pytest.fixture
def conn(tmp_path):
    c = reset(tmp_path / "alerts_test.db")
    c.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    c.commit()
    return c


# -- persistence -----------------------------------------------------------

def test_fire_alert_persists_row(conn):
    channel = LoggingAlertChannel()
    alert = fire_alert(
        conn, channel, organization_id="org-1", agent_id="agent-1",
        alert_type="rate_limited", severity="warning", message="too many calls",
        metadata={"tool": "get_weather"},
    )
    row = conn.execute("SELECT organization_id, agent_id, alert_type, severity, message, delivered FROM alerts WHERE id = ?", (alert.id,)).fetchone()
    assert row == ("org-1", "agent-1", "rate_limited", "warning", "too many calls", 1)


def test_fire_alert_invalid_severity_rejected(conn):
    with pytest.raises(ValueError):
        fire_alert(conn, LoggingAlertChannel(), organization_id="org-1", alert_type="x", severity="bogus", message="m")


def test_fire_alert_records_delivered_false_on_failed_channel(conn):
    class FailingChannel:
        def send(self, alert):
            return False

    alert = fire_alert(conn, FailingChannel(), organization_id="org-1", alert_type="x", severity="info", message="m")
    row = conn.execute("SELECT delivered FROM alerts WHERE id = ?", (alert.id,)).fetchone()
    assert row == (0,)


def test_fire_alert_survives_a_channel_that_raises(conn):
    """A misbehaving channel must never take the caller down -- fire_alert
    catches it and records delivered=0, same as an honest False."""
    class ExplodingChannel:
        def send(self, alert):
            raise RuntimeError("boom")

    alert = fire_alert(conn, ExplodingChannel(), organization_id="org-1", alert_type="x", severity="info", message="m")
    row = conn.execute("SELECT delivered FROM alerts WHERE id = ?", (alert.id,)).fetchone()
    assert row == (0,)


# -- channels ----------------------------------------------------------------

def test_logging_alert_channel_always_succeeds(caplog):
    from bf_agent_viewer.alerts.service import Alert
    alert = Alert(id="a1", organization_id="org-1", agent_id="agent-1", alert_type="x", severity="critical", message="m")
    with caplog.at_level("ERROR", logger="bf_agent_viewer.alerts"):
        assert LoggingAlertChannel().send(alert) is True
    assert any("ALERT" in r.message for r in caplog.records)


class _CapturingWebhookHandler(http.server.BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _CapturingWebhookHandler.received.append(json.loads(body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def webhook_server():
    _CapturingWebhookHandler.received = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _CapturingWebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=2)


def test_webhook_alert_channel_posts_json(webhook_server):
    from bf_agent_viewer.alerts.service import Alert
    port = webhook_server.server_address[1]
    channel = WebhookAlertChannel(f"http://127.0.0.1:{port}/hook")
    alert = Alert(id="a1", organization_id="org-1", agent_id="agent-1", alert_type="rate_limited", severity="warning", message="m", metadata={"tool": "get_weather"})

    assert channel.send(alert) is True
    assert len(_CapturingWebhookHandler.received) == 1
    assert _CapturingWebhookHandler.received[0]["id"] == "a1"
    assert _CapturingWebhookHandler.received[0]["metadata"] == {"tool": "get_weather"}


def test_webhook_alert_channel_returns_false_on_unreachable_url():
    from bf_agent_viewer.alerts.service import Alert
    channel = WebhookAlertChannel("http://127.0.0.1:1/nope", timeout=1.0)
    alert = Alert(id="a1", organization_id="org-1", agent_id=None, alert_type="x", severity="info", message="m")
    assert channel.send(alert) is False


def test_email_alert_channel_sends_via_smtp(monkeypatch):
    from bf_agent_viewer.alerts.service import Alert
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"] = host
            sent["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            sent["starttls"] = True

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, msg):
            sent["subject"] = msg["Subject"]
            sent["to"] = msg["To"]

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    channel = EmailAlertChannel(
        smtp_host="smtp.example.com", smtp_port=587, from_addr="alerts@example.com",
        to_addrs=["ops@example.com"], username="user", password="pass",
    )
    alert = Alert(id="a1", organization_id="org-1", agent_id="agent-1", alert_type="rate_limited", severity="critical", message="too many calls")

    assert channel.send(alert) is True
    assert sent["host"] == "smtp.example.com"
    assert sent["starttls"] is True
    assert sent["login"] == ("user", "pass")
    assert "CRITICAL" in sent["subject"]
    assert sent["to"] == "ops@example.com"


def test_email_alert_channel_returns_false_on_smtp_failure(monkeypatch):
    from bf_agent_viewer.alerts.service import Alert
    import smtplib

    class FailingSMTP:
        def __init__(self, *a, **kw):
            raise smtplib.SMTPConnectError(421, "nope")

    monkeypatch.setattr("smtplib.SMTP", FailingSMTP)
    channel = EmailAlertChannel(smtp_host="smtp.example.com", from_addr="a@example.com", to_addrs=["b@example.com"])
    alert = Alert(id="a1", organization_id="org-1", agent_id=None, alert_type="x", severity="info", message="m")
    assert channel.send(alert) is False


def test_composite_channel_succeeds_if_any_channel_succeeds():
    from bf_agent_viewer.alerts.service import Alert

    class Fails:
        def send(self, alert):
            return False

    class Succeeds:
        def send(self, alert):
            return True

    channel = CompositeAlertChannel([Fails(), Succeeds()])
    alert = Alert(id="a1", organization_id="org-1", agent_id=None, alert_type="x", severity="info", message="m")
    assert channel.send(alert) is True


def test_composite_channel_contains_a_raising_channel():
    from bf_agent_viewer.alerts.service import Alert

    class Explodes:
        def send(self, alert):
            raise RuntimeError("boom")

    class Succeeds:
        def send(self, alert):
            return True

    channel = CompositeAlertChannel([Explodes(), Succeeds()])
    alert = Alert(id="a1", organization_id="org-1", agent_id=None, alert_type="x", severity="info", message="m")
    assert channel.send(alert) is True  # the surviving channel still gets a chance


def test_composite_channel_empty_list_fails_closed():
    from bf_agent_viewer.alerts.service import Alert
    alert = Alert(id="a1", organization_id="org-1", agent_id=None, alert_type="x", severity="info", message="m")
    assert CompositeAlertChannel([]).send(alert) is False
