"""Credential issue/renew/revoke are written to the tamper-evident chain,
never with the raw token, and chaining stays intact when a second writer
(the CLI) logs while a gateway-style writer holds its own in-memory tip."""
import json

import pytest

from bf_agent_viewer.db import connect, reset
from bf_agent_viewer.events import last_hash, log_event, verify_chain
from bf_agent_viewer.identity import issue_token, register_identity, renew_token, revoke_token


@pytest.fixture
def conn(tmp_path):
    c = reset(tmp_path / "c.db")
    c.execute("INSERT INTO organizations (id, name) VALUES ('org-1','Org')")
    c.execute("INSERT INTO humans (id, organization_id, name) VALUES ('h1','org-1','H')")
    register_identity(c, organization_id="org-1", agent_id="a1", agent_name="A", owner_human_id="h1",
                      subject="a1", granted_scope=["t"])
    return c


def _events(conn, like):
    return conn.execute("SELECT event_type, agent_id, organization_id, source, metadata FROM events "
                        "WHERE event_type LIKE ? ORDER BY rowid", (like,)).fetchall()


def test_issue_renew_revoke_each_log_an_event_without_the_raw_token(conn):
    token = issue_token(conn, agent_id="a1", ttl_seconds=60)
    renew_token(conn, token, ttl_seconds=120)
    revoke_token(conn, token, actor_human_id="h1")
    rows = _events(conn, "credential.%")
    assert [r[0] for r in rows] == ["credential.issued", "credential.renewed", "credential.revoked"]
    assert all(r[1] == "a1" and r[2] == "org-1" for r in rows)
    assert all(token not in r[4] for r in rows)                 # no secret in the log
    assert all(json.loads(r[4])["credential"] for r in rows)    # but a fingerprint is there
    actor = conn.execute("SELECT actor_human_id FROM events WHERE event_type='credential.revoked'").fetchone()[0]
    assert actor == "h1"
    assert verify_chain(conn) == (True, None)


def test_failed_revoke_or_renew_logs_nothing(conn):
    token = issue_token(conn, agent_id="a1")
    revoke_token(conn, token)
    n = len(_events(conn, "credential.%"))
    with pytest.raises(ValueError):
        revoke_token(conn, token)
    with pytest.raises(ValueError):
        renew_token(conn, token, ttl_seconds=10)
    assert len(_events(conn, "credential.%")) == n


def test_two_writers_do_not_fork_the_chain(conn, tmp_path):
    """A gateway-style writer caches its tip; a CLI-style second connection
    revokes a credential in between. Both chain from the DB tip, so the
    chain still verifies."""
    token = issue_token(conn, agent_id="a1")
    cli = connect(tmp_path / "c.db")                      # separate connection, like the CLI process
    _, tip = log_event(conn, organization_id="org-1", agent_id="a1", session_id=None, actor_human_id=None,
                       event_type="tool.called", action="read", prev_hash=last_hash(conn))
    conn.commit()
    revoke_token(cli, token)                              # writes via the DB tip, not conn's cached tip
    log_event(conn, organization_id="org-1", agent_id="a1", session_id=None, actor_human_id=None,
              event_type="tool.called", action="read", prev_hash=None)   # gateway path chains from DB tip
    conn.commit()
    assert verify_chain(conn) == (True, None)
