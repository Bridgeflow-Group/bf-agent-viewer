from bf_agent_viewer.db import reset
from bf_agent_viewer.events import last_hash, log_event, verify_chain


def test_hash_chain_verifies_clean(tmp_path):
    conn = reset(tmp_path / "test.db")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute(
        "INSERT INTO agents (id, organization_id, name, owner_id) "
        "VALUES ('agent-1', 'org-1', 'Agent', 'human-1')"
    )
    prev = last_hash(conn)
    for i in range(5):
        _, prev = log_event(
            conn, organization_id="org-1", agent_id="agent-1", session_id=None,
            actor_human_id=None, event_type="tool.called", action="read",
            tool_id=f"tool-{i}", metadata={"i": i}, prev_hash=prev,
        )
    conn.commit()
    ok, bad_id = verify_chain(conn)
    assert ok
    assert bad_id is None


def test_hash_chain_detects_tampering(tmp_path):
    conn = reset(tmp_path / "test.db")
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute(
        "INSERT INTO agents (id, organization_id, name, owner_id) "
        "VALUES ('agent-1', 'org-1', 'Agent', 'human-1')"
    )
    prev = last_hash(conn)
    ids = []
    for i in range(3):
        eid, prev = log_event(
            conn, organization_id="org-1", agent_id="agent-1", session_id=None,
            actor_human_id=None, event_type="tool.called", action="read",
            tool_id=f"tool-{i}", metadata={"i": i}, prev_hash=prev,
        )
        ids.append(eid)
    conn.commit()

    # Tamper with the middle event's action after the fact.
    conn.execute("UPDATE events SET action = 'write' WHERE id = ?", (ids[1],))
    conn.commit()

    ok, bad_id = verify_chain(conn)
    assert not ok
    assert bad_id == ids[1]
