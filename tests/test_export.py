"""F-034/T-016: customer-facing compliance export. A filtered CSV export
of event history -- by agent, date range, and owner -- so a customer can
satisfy their own regulatory record-keeping obligations without
contacting us (OQ-011). Covers the shared export function, the CLI path
(`bf-agent-viewer export events`), and the console's `/export` page --
both surfaces call the same underlying export_events_csv(), so this
mostly proves that function is correct and that both surfaces wire it up
faithfully rather than duplicating the logic.
"""
from __future__ import annotations

import csv
import io

import pyotp
import pytest

starlette_testclient = pytest.importorskip("starlette.testclient")
TestClient = starlette_testclient.TestClient

from bf_agent_viewer.cli import main as cli_main
from bf_agent_viewer.console import auth, build_console
from bf_agent_viewer.db import reset
from bf_agent_viewer.events import last_hash, log_event
from bf_agent_viewer.identity import register_identity
from bf_agent_viewer.reports import CSV_HEADER, InvalidDateBoundError, export_events_csv

PASSWORD = "correct horse battery staple"


def _seed(conn):
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-2', 'Other Org')")
    conn.execute("INSERT INTO humans (id, organization_id, name, email) VALUES ('human-1', 'org-1', 'Ada', 'ada@example.com')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('human-2', 'org-1', 'Bea')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('human-3', 'org-2', 'Cid')")
    register_identity(
        conn, organization_id="org-1", agent_id="agent-a", agent_name="Agent A",
        owner_human_id="human-1", subject="agent-a", granted_scope=["get_weather"],
    )
    register_identity(
        conn, organization_id="org-1", agent_id="agent-b", agent_name="Agent B",
        owner_human_id="human-2", subject="agent-b", granted_scope=["get_weather"],
    )
    register_identity(
        conn, organization_id="org-2", agent_id="agent-c", agent_name="Agent C (other org)",
        owner_human_id="human-3", subject="agent-c", granted_scope=["get_weather"],
    )
    prev = last_hash(conn)
    _, prev = log_event(
        conn, organization_id="org-1", agent_id="agent-a", session_id=None, actor_human_id=None,
        event_type="tool.called", action="read", tool_id="get_weather", result="success",
        metadata={"city": "Lima"}, prev_hash=prev,
    )
    _, prev = log_event(
        conn, organization_id="org-1", agent_id="agent-b", session_id=None, actor_human_id=None,
        event_type="tool.called", action="read", tool_id="get_weather", result="success",
        metadata={"city": "Cusco"}, prev_hash=prev,
    )
    _, prev = log_event(
        conn, organization_id="org-2", agent_id="agent-c", session_id=None, actor_human_id=None,
        event_type="tool.called", action="read", tool_id="get_weather", result="success",
        metadata={"city": "Elsewhere"}, prev_hash=prev,
    )
    conn.commit()


def _rows(csv_text):
    return list(csv.DictReader(io.StringIO(csv_text)))


# -- export_events_csv (unit) ---------------------------------------------

def test_export_header_matches_csv_header_constant(tmp_path):
    conn = reset(tmp_path / "exp1.db")
    _seed(conn)
    csv_text = export_events_csv(conn)
    header = next(csv.reader(io.StringIO(csv_text)))
    assert header == CSV_HEADER


def test_export_with_no_filters_returns_every_event(tmp_path):
    conn = reset(tmp_path / "exp2.db")
    _seed(conn)
    rows = _rows(export_events_csv(conn))
    assert len(rows) == 3


def test_export_filters_by_organization(tmp_path):
    conn = reset(tmp_path / "exp3.db")
    _seed(conn)
    rows = _rows(export_events_csv(conn, organization_id="org-1"))
    assert {r["agent_id"] for r in rows} == {"agent-a", "agent-b"}


def test_export_filters_by_agent(tmp_path):
    conn = reset(tmp_path / "exp4.db")
    _seed(conn)
    rows = _rows(export_events_csv(conn, agent_id="agent-a"))
    assert len(rows) == 1
    assert rows[0]["agent_id"] == "agent-a"
    assert rows[0]["agent_name"] == "Agent A"
    assert rows[0]["owner_name"] == "Ada"


def test_export_filters_by_owner(tmp_path):
    conn = reset(tmp_path / "exp5.db")
    _seed(conn)
    rows = _rows(export_events_csv(conn, owner_human_id="human-2"))
    assert len(rows) == 1
    assert rows[0]["agent_id"] == "agent-b"


def test_export_org_filter_cannot_be_bypassed_by_cross_org_agent_id(tmp_path):
    """T-014-style check: an export scoped to org-1 must not leak an
    org-2 event even if the caller supplies org-2's own agent_id."""
    conn = reset(tmp_path / "exp6.db")
    _seed(conn)
    rows = _rows(export_events_csv(conn, organization_id="org-1", agent_id="agent-c"))
    assert rows == []


def test_export_date_range_bare_date_bounds_are_inclusive(tmp_path):
    conn = reset(tmp_path / "exp7.db")
    _seed(conn)
    # All three seeded events happen "now" -- a bare-date range covering
    # today (UTC) should include all of them; a range that ends yesterday
    # should include none.
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    rows_today = _rows(export_events_csv(conn, start=today, end=today))
    assert len(rows_today) == 3

    rows_before = _rows(export_events_csv(conn, start=yesterday, end=yesterday))
    assert rows_before == []


def test_export_rejects_malformed_date(tmp_path):
    conn = reset(tmp_path / "exp8.db")
    _seed(conn)
    with pytest.raises(InvalidDateBoundError):
        export_events_csv(conn, start="not-a-date")


def test_export_includes_content_hash_for_independent_verification(tmp_path):
    conn = reset(tmp_path / "exp9.db")
    _seed(conn)
    rows = _rows(export_events_csv(conn, agent_id="agent-a"))
    assert rows[0]["content_hash"]


# -- CLI (bf-agent-viewer export events) -----------------------------------

def test_cli_export_events_to_stdout(tmp_path, capsys):
    db = str(tmp_path / "cli_exp.db")
    conn = reset(db)
    _seed(conn)
    conn.close()

    cli_main(["export", "events", "--db", db, "--org", "org-1"])
    out = capsys.readouterr().out
    rows = _rows(out)
    assert {r["agent_id"] for r in rows} == {"agent-a", "agent-b"}


def test_cli_export_events_to_file(tmp_path):
    db = str(tmp_path / "cli_exp2.db")
    out_path = tmp_path / "events.csv"
    conn = reset(db)
    _seed(conn)
    conn.close()

    cli_main(["export", "events", "--db", db, "--org", "org-1", "--agent-id", "agent-b", "--out", str(out_path)])
    rows = _rows(out_path.read_text())
    assert len(rows) == 1
    assert rows[0]["agent_id"] == "agent-b"


def test_cli_export_events_rejects_bad_date(tmp_path, capsys):
    db = str(tmp_path / "cli_exp3.db")
    conn = reset(db)
    _seed(conn)
    conn.close()

    with pytest.raises(SystemExit):
        cli_main(["export", "events", "--db", db, "--start", "banana"])
    assert "error" in capsys.readouterr().out.lower()


# -- console /export -------------------------------------------------------

def _login(client, conn, *, email="ada@example.com", password=PASSWORD):
    result = auth.start_login(conn, email=email, password=password)
    assert result.status == "needs_enrollment"
    secret, _uri = auth.totp_provisioning_uri(conn, result.human_id)
    code = pyotp.TOTP(secret).now()
    client.cookies.set("bf_console_pending", result.pending_token)
    resp = client.post("/login/enroll", data={"code": code}, follow_redirects=False)
    assert resp.status_code == 303


@pytest.fixture
def client(tmp_path):
    conn = reset(tmp_path / "export_console.db", check_same_thread=False)
    _seed(conn)
    auth.provision_console_user(conn, human_id="human-1", password=PASSWORD, account_label="ada@example.com")
    conn.commit()
    app = build_console(conn, organization_id="org-1")
    c = TestClient(app)
    _login(c, conn)
    return c, conn


def test_export_page_renders_form(client):
    c, _conn = client
    resp = c.get("/export")
    assert resp.status_code == 200
    assert "Compliance export" in resp.text
    assert "Agent A" in resp.text  # agent picker populated
    assert "Ada" in resp.text  # owner picker populated


def test_export_download_returns_real_csv_attachment(client):
    c, _conn = client
    resp = c.get("/export", params={"download": "1"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    rows = _rows(resp.text)
    # Console is scoped to org-1 -- only agent-a and agent-b's events.
    assert {r["agent_id"] for r in rows} == {"agent-a", "agent-b"}


def test_export_download_filtered_by_agent(client):
    c, _conn = client
    resp = c.get("/export", params={"download": "1", "agent_id": "agent-a"})
    rows = _rows(resp.text)
    assert len(rows) == 1
    assert rows[0]["agent_id"] == "agent-a"


def test_export_download_cannot_leak_another_organization(client):
    """The console is built with organization_id='org-1' -- a download
    request naming org-2's own agent_id must not return org-2's event,
    mirroring T-014's org-isolation guarantee for every other console
    query."""
    c, _conn = client
    resp = c.get("/export", params={"download": "1", "agent_id": "agent-c"})
    assert _rows(resp.text) == []


def test_export_download_bad_date_shows_error_not_a_crash(client):
    c, _conn = client
    resp = c.get("/export", params={"download": "1", "start": "not-a-date"})
    assert resp.status_code == 400
    assert "not a recognized date" in resp.text
