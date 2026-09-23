"""F-042: gateway-outage gap marker + alert (bf_agent_viewer/gateway/resilience.py).

Unit-level: exercises check_and_log_gap() directly against a real SQLite
connection (via db.reset), with a capturing alert channel standing in for
a real webhook/email/logging channel -- fire_alert()'s own delivery
mechanics are already covered by test_alerts.py, so these tests focus on
the gap-detection logic itself: no-prior-events, recent-event, and
past-threshold cases, plus the warning/critical severity split.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bf_agent_viewer.alerts import Alert
from bf_agent_viewer.db import reset
from bf_agent_viewer.events import last_hash, log_event
from bf_agent_viewer.gateway.resilience import (
    CRITICAL_GAP_THRESHOLD_SECONDS,
    GAP_MARKER_AGENT_ID,
    check_and_log_gap,
)


class CapturingAlertChannel:
    def __init__(self):
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


def _make_conn(tmp_path):
    conn = reset(tmp_path / "resilience_test.db")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.commit()
    return conn


def _log_one_event(conn, *, occurred_at_override: str | None = None):
    """Logs a real event, optionally back-dating occurred_at afterward
    (log_event always stamps "now" -- back-dating via UPDATE is the only
    way to simulate an old last-event without sleeping in the test)."""
    event_id, new_hash = log_event(
        conn, organization_id="org-1", agent_id="agent-1", session_id=None,
        actor_human_id=None, event_type="tool.call", action="get_weather",
        prev_hash=last_hash(conn),
    )
    conn.commit()
    if occurred_at_override is not None:
        conn.execute("UPDATE events SET occurred_at = ? WHERE id = ?", (occurred_at_override, event_id))
        conn.commit()
    return event_id


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_no_prior_events_is_not_a_gap(tmp_path):
    conn = _make_conn(tmp_path)
    channel = CapturingAlertChannel()
    result = check_and_log_gap(conn, organization_id="org-1", alert_channel=channel)
    assert result is None
    assert channel.sent == []
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_recent_event_is_not_a_gap(tmp_path):
    conn = _make_conn(tmp_path)
    now = datetime.now(timezone.utc)
    _log_one_event(conn, occurred_at_override=_iso(now - timedelta(seconds=5)))
    channel = CapturingAlertChannel()
    result = check_and_log_gap(conn, organization_id="org-1", alert_channel=channel, gap_threshold_seconds=60.0, now=now)
    assert result is None
    assert channel.sent == []
    # still just the one event -- no gap.gateway row added
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_old_event_past_threshold_logs_gap_and_fires_alert(tmp_path):
    conn = _make_conn(tmp_path)
    now = datetime.now(timezone.utc)
    last_event_id = _log_one_event(conn, occurred_at_override=_iso(now - timedelta(seconds=300)))
    channel = CapturingAlertChannel()

    result = check_and_log_gap(conn, organization_id="org-1", alert_channel=channel, gap_threshold_seconds=60.0, now=now)

    assert result is not None
    row = conn.execute(
        "SELECT event_type, agent_id, result, metadata FROM events WHERE id = ?", (result,)
    ).fetchone()
    assert row[0] == "gateway.gap"
    assert row[1] == GAP_MARKER_AGENT_ID
    assert row[2] == "gap_detected"
    import json
    metadata = json.loads(row[3])
    assert metadata["last_event_before_gap"] == last_event_id
    assert 295 <= metadata["duration_seconds"] <= 305

    assert len(channel.sent) == 1
    assert channel.sent[0].alert_type == "gateway_gap"
    assert channel.sent[0].severity == "warning"

    alert_row = conn.execute(
        "SELECT alert_type, severity, delivered FROM alerts WHERE organization_id = 'org-1'"
    ).fetchone()
    assert alert_row == ("gateway_gap", "warning", 1)


def test_gap_past_critical_threshold_escalates_severity(tmp_path):
    conn = _make_conn(tmp_path)
    now = datetime.now(timezone.utc)
    _log_one_event(conn, occurred_at_override=_iso(now - timedelta(seconds=CRITICAL_GAP_THRESHOLD_SECONDS + 60)))
    channel = CapturingAlertChannel()

    result = check_and_log_gap(conn, organization_id="org-1", alert_channel=channel, gap_threshold_seconds=60.0, now=now)

    assert result is not None
    assert channel.sent[0].severity == "critical"


def test_gap_event_extends_the_chain_not_a_fork(tmp_path):
    """The logged gap event's prev_hash must be the real chain tip -- i.e.
    last_hash(conn) after check_and_log_gap must be the gap event's own
    new hash, not still pointing at the pre-gap event."""
    conn = _make_conn(tmp_path)
    now = datetime.now(timezone.utc)
    _log_one_event(conn, occurred_at_override=_iso(now - timedelta(seconds=300)))
    tip_before = last_hash(conn)

    check_and_log_gap(conn, organization_id="org-1", alert_channel=CapturingAlertChannel(), gap_threshold_seconds=60.0, now=now)

    tip_after = last_hash(conn)
    assert tip_after != tip_before


def test_no_alert_channel_falls_back_to_logging(tmp_path, caplog):
    """Passing alert_channel=None must not raise -- it falls back to
    LoggingAlertChannel, mirroring GatewayMiddleware's own default."""
    conn = _make_conn(tmp_path)
    now = datetime.now(timezone.utc)
    _log_one_event(conn, occurred_at_override=_iso(now - timedelta(seconds=300)))

    with caplog.at_level("WARNING"):
        result = check_and_log_gap(conn, organization_id="org-1", alert_channel=None, gap_threshold_seconds=60.0, now=now)

    assert result is not None
    alert_row = conn.execute("SELECT delivered FROM alerts WHERE organization_id = 'org-1'").fetchone()
    assert alert_row == (1,)
