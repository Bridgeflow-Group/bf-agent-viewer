"""Regression tests: full-row event hashing (v2) and --require-token."""
import json
import sqlite3

import pytest

from bf_agent_viewer.db import reset
from bf_agent_viewer.events import log_event, verify_chain


def _seed(tmp_path):
    conn = reset(str(tmp_path / "t.db"))
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1','Org')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('h1','org-1','H')")
    conn.execute("INSERT INTO agents (id, organization_id, name, owner_id) VALUES ('a1','org-1','A','h1')")
    conn.commit()
    return conn


def _write_events(conn):
    prev = "GENESIS"
    for i in range(3):
        _, prev = log_event(
            conn, organization_id="org-1", agent_id="a1", session_id=None, actor_human_id=None,
            event_type="tool.blocked", action="write", tool_id="tool-x", result="denied",
            metadata={"arguments": {"n": i}}, prev_hash=prev,
        )
    conn.commit()


@pytest.mark.parametrize("column,value", [
    ("result", "success"), ("tool_id", "other-tool"), ("metadata", '{"arguments": {"n": 99}}'),
    ("actor_human_id", "h1"), ("source", "forged"), ("organization_id", "org-2"),
    ("environment", "dev"), ("request_id", "forged"),
])
def test_editing_any_covered_column_breaks_the_chain(tmp_path, column, value):
    conn = _seed(tmp_path)
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-2','Org2')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('h2','org-1','H2')")
    _write_events(conn)
    assert verify_chain(conn) == (True, None)
    conn.execute(f"UPDATE events SET {column} = ? WHERE rowid = 2", (value,))
    conn.commit()
    ok, bad = verify_chain(conn)
    assert not ok and bad is not None


def test_legacy_v1_rows_still_verify_then_v2_rows_chain_on(tmp_path):
    import hashlib
    conn = _seed(tmp_path)
    payload = {"id": "e-old", "occurred_at": "2026-09-01T00:00:00Z", "agent_id": "a1",
               "event_type": "tool.called", "action": "read", "resource_id": None}
    h = hashlib.sha256(("GENESIS" + json.dumps(payload, sort_keys=True)).encode()).hexdigest()
    conn.execute(
        "INSERT INTO events (id, event_version, occurred_at, organization_id, agent_id, event_type, action, content_hash) "
        "VALUES ('e-old','v1','2026-09-01T00:00:00Z','org-1','a1','tool.called','read',?)", (h,))
    log_event(conn, organization_id="org-1", agent_id="a1", session_id=None, actor_human_id=None,
              event_type="tool.called", action="read", prev_hash=h)
    conn.commit()
    assert verify_chain(conn) == (True, None)


def test_unknown_event_version_fails_closed(tmp_path):
    conn = _seed(tmp_path)
    _write_events(conn)
    conn.execute("UPDATE events SET event_version = 'v9' WHERE rowid = 1")
    conn.commit()
    assert verify_chain(conn)[0] is False
