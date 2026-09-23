"""F-046: agent online/offline status indicator on the console dashboard
and agent detail view. Unit-level for the derivation function itself,
plus real requests through the console (queries.py + templates) so a
template-rendering regression would actually be caught, not just the
computed value."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pyotp
import pytest

starlette_testclient = pytest.importorskip("starlette.testclient")
TestClient = starlette_testclient.TestClient

from bf_agent_viewer.console import auth, build_console, queries
from bf_agent_viewer.console.queries import derive_online_status
from bf_agent_viewer.db import reset
from bf_agent_viewer.events import last_hash, log_event
from bf_agent_viewer.identity import register_identity

PASSWORD = "correct horse battery staple"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# -- derive_online_status (unit) --------------------------------------

def test_no_activity_is_never_not_offline():
    assert derive_online_status(None) == "never"


def test_recent_activity_is_online():
    now = datetime.now(timezone.utc)
    last = _iso(now - timedelta(seconds=10))
    assert derive_online_status(last, now=now, threshold_seconds=300.0) == "online"


def test_stale_activity_is_offline():
    now = datetime.now(timezone.utc)
    last = _iso(now - timedelta(seconds=600))
    assert derive_online_status(last, now=now, threshold_seconds=300.0) == "offline"


def test_exactly_at_threshold_is_offline():
    """elapsed < threshold is the online condition, so elapsed == threshold
    is offline, not a coin flip -- pin the boundary down explicitly."""
    now = datetime.now(timezone.utc)
    last = _iso(now - timedelta(seconds=300))
    assert derive_online_status(last, now=now, threshold_seconds=300.0) == "offline"


def test_threshold_is_configurable():
    now = datetime.now(timezone.utc)
    last = _iso(now - timedelta(seconds=120))
    assert derive_online_status(last, now=now, threshold_seconds=60.0) == "offline"
    assert derive_online_status(last, now=now, threshold_seconds=300.0) == "online"


# -- queries.list_agents / get_agent (real DB) -------------------------

def _seed(conn):
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute(
        "INSERT INTO humans (id, organization_id, name, email) VALUES ('human-1', 'org-1', 'Ada Owner', 'ada@example.com')"
    )
    auth.provision_console_user(conn, human_id="human-1", password=PASSWORD, account_label="ada@example.com")
    register_identity(
        conn, organization_id="org-1", agent_id="agent-online", agent_name="Online Agent",
        owner_human_id="human-1", subject="agent-online", granted_scope=["get_weather"],
    )
    register_identity(
        conn, organization_id="org-1", agent_id="agent-offline", agent_name="Offline Agent",
        owner_human_id="human-1", subject="agent-offline", granted_scope=["get_weather"],
    )
    register_identity(
        conn, organization_id="org-1", agent_id="agent-never", agent_name="Never Ran Agent",
        owner_human_id="human-1", subject="agent-never", granted_scope=["get_weather"],
    )
    prev = last_hash(conn)
    event_id, prev = log_event(
        conn, organization_id="org-1", agent_id="agent-online", session_id=None,
        actor_human_id="human-1", event_type="tool.called", action="call",
        tool_id="get_weather", result="success", metadata={}, prev_hash=prev,
    )
    event_id2, _ = log_event(
        conn, organization_id="org-1", agent_id="agent-offline", session_id=None,
        actor_human_id="human-1", event_type="tool.called", action="call",
        tool_id="get_weather", result="success", metadata={}, prev_hash=prev,
    )
    conn.commit()
    stale = _iso(datetime.now(timezone.utc) - timedelta(seconds=9999))
    conn.execute("UPDATE events SET occurred_at = ? WHERE id = ?", (stale, event_id2))
    conn.commit()


def test_list_agents_derives_status_per_agent(tmp_path):
    conn = reset(tmp_path / "status_test.db")
    _seed(conn)
    agents = {a.id: a for a in queries.list_agents(conn, online_threshold_seconds=300.0)}
    assert agents["agent-online"].online_status == "online"
    assert agents["agent-offline"].online_status == "offline"
    assert agents["agent-never"].online_status == "never"


def test_get_agent_derives_status(tmp_path):
    conn = reset(tmp_path / "status_test2.db")
    _seed(conn)
    detail = queries.get_agent(conn, "agent-online", online_threshold_seconds=300.0)
    assert detail.online_status == "online"
    detail2 = queries.get_agent(conn, "agent-never", online_threshold_seconds=300.0)
    assert detail2.online_status == "never"


# -- real console requests (dashboard + agent detail render the badge) --

def _login(client, conn):
    result = auth.start_login(conn, email="ada@example.com", password=PASSWORD)
    assert result.status == "needs_enrollment"
    secret, _uri = auth.totp_provisioning_uri(conn, result.human_id)
    code = pyotp.TOTP(secret).now()
    client.cookies.set("bf_console_pending", result.pending_token)
    resp = client.post("/login/enroll", data={"code": code}, follow_redirects=False)
    assert resp.status_code == 303


def test_dashboard_shows_online_offline_badges(tmp_path):
    conn = reset(tmp_path / "status_console.db", check_same_thread=False)
    _seed(conn)
    app = build_console(conn, online_threshold_seconds=300.0)
    c = TestClient(app)
    _login(c, conn)
    resp = c.get("/")
    assert resp.status_code == 200
    assert 'badge online">online' in resp.text
    assert 'badge offline">offline' in resp.text
    assert 'badge never">never' in resp.text


def test_agent_detail_shows_online_badge(tmp_path):
    conn = reset(tmp_path / "status_console2.db", check_same_thread=False)
    _seed(conn)
    app = build_console(conn, online_threshold_seconds=300.0)
    c = TestClient(app)
    _login(c, conn)
    resp = c.get("/agents/agent-online")
    assert resp.status_code == 200
    assert 'badge online">online' in resp.text


def test_custom_threshold_changes_classification(tmp_path):
    """The agent-offline fixture's event is ~9999s stale -- with a huge
    threshold it should read as online instead, proving the threshold is
    actually wired through end to end, not hardcoded."""
    conn = reset(tmp_path / "status_console3.db", check_same_thread=False)
    _seed(conn)
    app = build_console(conn, online_threshold_seconds=999999.0)
    c = TestClient(app)
    _login(c, conn)
    resp = c.get("/agents/agent-offline")
    assert resp.status_code == 200
    assert 'badge online">online' in resp.text
