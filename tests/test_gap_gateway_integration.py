"""Real integration test for F-042: build_gateway() itself must run the
gap check at startup, before request-serving middleware picks up the
chain tip. Seeds a stale last-event timestamp, calls the real
build_gateway() (no gateway.run_async needed -- the gap check happens
synchronously during the build, not per-request), and asserts a
gateway.gap event and a delivered gateway_gap alert both exist and that
the configured channel actually received it."""
import os
from datetime import datetime, timedelta, timezone

from bf_agent_viewer.alerts import Alert
from bf_agent_viewer.db import reset
from bf_agent_viewer.events import last_hash, log_event
from bf_agent_viewer.gateway import build_gateway
from bf_agent_viewer.identity import issue_token, register_identity

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_SCRIPT = os.path.join(HERE, "fixtures", "demo_backend.py")


class CapturingAlertChannel:
    def __init__(self):
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


def test_build_gateway_marks_a_stale_startup_gap(tmp_path):
    conn = reset(tmp_path / "gw_gap.db")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('human-1', 'org-1', 'Owner')")
    register_identity(
        conn, organization_id="org-1", agent_id="agent-1", agent_name="Agent",
        owner_human_id="human-1", subject="agent-1", granted_scope=["get_weather"],
    )
    issue_token(conn, agent_id="agent-1")

    # Seed one real event, then back-date it well past the threshold --
    # simulates "the gateway went down after this and is only now coming
    # back up".
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=600)
    event_id, _ = log_event(
        conn, organization_id="org-1", agent_id="agent-1", session_id=None,
        actor_human_id=None, event_type="tool.call", action="get_weather",
        prev_hash=last_hash(conn),
    )
    conn.commit()
    conn.execute(
        "UPDATE events SET occurred_at = ? WHERE id = ?",
        (stale_time.strftime("%Y-%m-%dT%H:%M:%SZ"), event_id),
    )
    conn.commit()

    channel = CapturingAlertChannel()
    gateway, middleware = build_gateway(
        conn, backend_script=BACKEND_SCRIPT, organization_id="org-1", write_tools=set(),
        alert_channel=channel, prefer_container=False, gap_threshold_seconds=60.0,
    )

    gap_row = conn.execute(
        "SELECT event_type, agent_id, result FROM events WHERE event_type = 'gateway.gap'"
    ).fetchone()
    assert gap_row == ("gateway.gap", "agent-gateway", "gap_detected")

    alert_row = conn.execute(
        "SELECT alert_type, severity, delivered FROM alerts WHERE organization_id = 'org-1' AND alert_type = 'gateway_gap'"
    ).fetchone()
    assert alert_row == ("gateway_gap", "warning", 1)

    assert len(channel.sent) == 1
    assert channel.sent[0].alert_type == "gateway_gap"

    # The middleware's own chain tip must be the gap event's hash, not the
    # pre-gap event's -- i.e. the gap check ran before GatewayMiddleware
    # was constructed, so it isn't a fork off to the side.
    assert middleware.running_hash == last_hash(conn)


def test_build_gateway_does_not_mark_a_gap_on_a_fresh_restart(tmp_path):
    conn = reset(tmp_path / "gw_nogap.db")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('human-1', 'org-1', 'Owner')")
    register_identity(
        conn, organization_id="org-1", agent_id="agent-1", agent_name="Agent",
        owner_human_id="human-1", subject="agent-1", granted_scope=["get_weather"],
    )
    issue_token(conn, agent_id="agent-1")

    log_event(
        conn, organization_id="org-1", agent_id="agent-1", session_id=None,
        actor_human_id=None, event_type="tool.call", action="get_weather",
        prev_hash=last_hash(conn),
    )
    conn.commit()

    channel = CapturingAlertChannel()
    build_gateway(
        conn, backend_script=BACKEND_SCRIPT, organization_id="org-1", write_tools=set(),
        alert_channel=channel, prefer_container=False, gap_threshold_seconds=60.0,
    )

    assert conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'gateway.gap'").fetchone()[0] == 0
    assert channel.sent == []
