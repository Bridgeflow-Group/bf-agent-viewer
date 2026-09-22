"""Real requests through Starlette's TestClient against a seeded SQLite
DB -- assert actual rendered page content, not just a 200 status.

Every test here goes through the real login flow (auth.py), not a bypass
-- if the auth wiring in app.py ever breaks, these tests should be the
ones that catch it, not just test_console_auth.py."""
import pyotp
import pytest

starlette_testclient = pytest.importorskip("starlette.testclient")
TestClient = starlette_testclient.TestClient

from bf_agent_viewer.console import auth, build_console
from bf_agent_viewer.db import reset
from bf_agent_viewer.events import last_hash, log_event
from bf_agent_viewer.identity import register_identity

PASSWORD = "correct horse battery staple"


def _login(client, conn, *, email="ada@example.com", password=PASSWORD):
    """Drives the real two-step login (password, then first-time TOTP
    enrollment) and leaves the client holding a valid session cookie."""
    result = auth.start_login(conn, email=email, password=password)
    assert result.status == "needs_enrollment"
    secret, _uri = auth.totp_provisioning_uri(conn, result.human_id)
    code = pyotp.TOTP(secret).now()
    client.cookies.set("bf_console_pending", result.pending_token)
    resp = client.post("/login/enroll", data={"code": code}, follow_redirects=False)
    assert resp.status_code == 303
    assert "bf_console_session" in client.cookies


def _seed(conn):
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute(
        "INSERT INTO humans (id, organization_id, name, email) VALUES ('human-1', 'org-1', 'Ada Owner', 'ada@example.com')"
    )
    auth.provision_console_user(conn, human_id="human-1", password=PASSWORD, account_label="ada@example.com")
    parent = register_identity(
        conn, organization_id="org-1", agent_id="agent-orchestrator", agent_name="Orchestrator",
        owner_human_id="human-1", subject="agent-orchestrator",
        granted_scope=["get_weather", "delete_record"],
    )
    register_identity(
        conn, organization_id="org-1", agent_id="agent-subagent", agent_name="Sub-agent",
        owner_human_id="human-1", subject="agent-subagent",
        granted_scope=["get_weather"], parent_identity_id=parent.identity_id,
    )
    prev = last_hash(conn)
    event_id, prev = log_event(
        conn, organization_id="org-1", agent_id="agent-orchestrator", session_id=None,
        actor_human_id="human-1", event_type="tool.called", action="call",
        tool_id="get_weather", result="success", metadata={"city": "Lima"}, prev_hash=prev,
    )
    log_event(
        conn, organization_id="org-1", agent_id="agent-subagent", session_id=None,
        actor_human_id=None, event_type="tool.blocked", action="call",
        tool_id="delete_record", result="blocked", metadata={"reason": "out of scope"},
        prev_hash=prev,
    )
    conn.commit()
    return event_id


@pytest.fixture
def client(tmp_path):
    conn = reset(tmp_path / "console_test.db", check_same_thread=False)
    event_id = _seed(conn)
    app = build_console(conn)
    c = TestClient(app)
    _login(c, conn)
    return c, event_id, conn


def test_dashboard_lists_agents_and_recent_events(client):
    c, _event_id, _conn = client
    resp = c.get("/")
    assert resp.status_code == 200
    assert "Orchestrator" in resp.text
    assert "Sub-agent" in resp.text
    assert "Ada Owner" in resp.text
    assert "get_weather" in resp.text
    assert "delete_record" in resp.text


def test_dashboard_shows_onboarding_when_no_agents(tmp_path):
    conn = reset(tmp_path / "empty.db", check_same_thread=False)
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute(
        "INSERT INTO humans (id, organization_id, name, email) VALUES ('human-1', 'org-1', 'Ada', 'ada@example.com')"
    )
    auth.provision_console_user(conn, human_id="human-1", password=PASSWORD, account_label="ada@example.com")
    app = build_console(conn)
    c = TestClient(app)
    _login(c, conn)
    resp = c.get("/")
    assert resp.status_code == 200
    assert "No agents registered yet" in resp.text
    assert "bf-agent-viewer register" in resp.text


def test_agent_detail_shows_delegation_and_scope(client):
    c, _event_id, conn = client
    resp = c.get("/agents/agent-subagent")
    assert resp.status_code == 200
    assert "Sub-agent" in resp.text
    # Delegation chain (F-026) surfaced in the UI.
    assert "Orchestrator" in resp.text
    # This sub-agent was only ever granted get_weather -- checked against
    # the actual data, not string-matched against the whole page, since
    # delete_record legitimately does appear elsewhere on this page (the
    # blocked call shows up in the activity table below).
    from bf_agent_viewer.console import queries
    agent = queries.get_agent(conn, "agent-subagent")
    assert agent.granted_scope == ["get_weather"]


def test_agent_detail_404_for_unknown_agent(client):
    c, _event_id, _conn = client
    resp = c.get("/agents/does-not-exist")
    assert resp.status_code == 404
    assert "No such agent" in resp.text


def test_event_detail_shows_full_record(client):
    c, event_id, _conn = client
    resp = c.get(f"/events/{event_id}")
    assert resp.status_code == 200
    assert "tool.called" in resp.text
    assert "get_weather" in resp.text
    assert "Lima" in resp.text  # metadata rendered


def test_event_detail_404_for_unknown_event(client):
    c, _event_id, _conn = client
    resp = c.get("/events/does-not-exist")
    assert resp.status_code == 404


def test_search_finds_agent_by_name(client):
    c, _event_id, _conn = client
    resp = c.get("/search", params={"q": "Sub-agent"})
    assert resp.status_code == 200
    assert "Sub-agent" in resp.text


def test_search_finds_event_by_tool(client):
    c, _event_id, _conn = client
    resp = c.get("/search", params={"q": "delete_record"})
    assert resp.status_code == 200
    assert "tool.blocked" in resp.text


def test_search_with_no_query_shows_prompt_not_everything(client):
    c, _event_id, _conn = client
    resp = c.get("/search")
    assert resp.status_code == 200
    assert "Enter a search term" in resp.text


def test_console_scoped_to_organization(tmp_path):
    conn = reset(tmp_path / "multi_org.db", check_same_thread=False)
    conn.executescript(
        """
        INSERT INTO organizations (id, name) VALUES ('org-a', 'Org A');
        INSERT INTO organizations (id, name) VALUES ('org-b', 'Org B');
        INSERT INTO humans (id, organization_id, name) VALUES ('h-a', 'org-a', 'A Owner');
        INSERT INTO humans (id, organization_id, name) VALUES ('h-b', 'org-b', 'B Owner');
        """
    )
    register_identity(
        conn, organization_id="org-a", agent_id="agent-a", agent_name="Agent A",
        owner_human_id="h-a", subject="agent-a", granted_scope=["get_weather"],
    )
    register_identity(
        conn, organization_id="org-b", agent_id="agent-b", agent_name="Agent B",
        owner_human_id="h-b", subject="agent-b", granted_scope=["get_weather"],
    )
    conn.execute(
        "UPDATE humans SET email = 'a@example.com' WHERE id = 'h-a'"
    )
    auth.provision_console_user(conn, human_id="h-a", password=PASSWORD, account_label="a@example.com")
    conn.commit()

    app = build_console(conn, organization_id="org-a")
    c = TestClient(app)
    _login(c, conn, email="a@example.com")
    resp = c.get("/")
    assert "Agent A" in resp.text
    assert "Agent B" not in resp.text


def test_unauthenticated_request_redirects_to_login(tmp_path):
    conn = reset(tmp_path / "unauth.db", check_same_thread=False)
    app = build_console(conn)
    resp = TestClient(app).get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
