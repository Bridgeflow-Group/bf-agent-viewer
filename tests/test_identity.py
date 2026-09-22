import pytest

from bf_agent_viewer.db import reset
from bf_agent_viewer.identity import (
    client_info_name,
    issue_token,
    load_token_registry,
    register_identity,
)


def _seed_org_human(conn):
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute(
        "INSERT INTO humans (id, organization_id, name) VALUES ('human-1', 'org-1', 'Owner')"
    )


def test_client_info_name_handles_dict_shape():
    # The modern on_discover path gives a plain dict -- the bug fixed
    # Sept 22 2026 was getattr() silently returning None on this shape.
    assert client_info_name({"name": "agent-x", "version": "1.0"}) == "agent-x"


def test_client_info_name_handles_object_shape():
    class FakeImplementation:
        name = "agent-y"
        version = "1.0"

    assert client_info_name(FakeImplementation()) == "agent-y"


def test_client_info_name_handles_none():
    assert client_info_name(None) is None


def test_register_identity_enforces_scope_narrowing(tmp_path):
    conn = reset(tmp_path / "test.db")
    _seed_org_human(conn)

    parent = register_identity(
        conn, organization_id="org-1", agent_id="agent-parent", agent_name="Parent",
        owner_human_id="human-1", subject="agent-parent",
        granted_scope=["get_weather", "read_file", "send_email", "delete_record"],
    )

    # Narrower than parent -- allowed.
    child = register_identity(
        conn, organization_id="org-1", agent_id="agent-child", agent_name="Child",
        owner_human_id="human-1", subject="agent-child",
        granted_scope=["get_weather", "read_file"],
        parent_identity_id=parent.identity_id,
    )
    assert child.parent_identity_id == parent.identity_id
    assert child.granted_scope == frozenset({"get_weather", "read_file"})

    # Includes a tool the parent was never granted -- must be rejected,
    # not silently allowed (this is genuinely wider, not a subset -- an
    # earlier version of this test used a strict-subset scope by mistake
    # and passed for the wrong reason; fixed to actually exercise the
    # rejection path).
    with pytest.raises(ValueError):
        register_identity(
            conn, organization_id="org-1", agent_id="agent-child2", agent_name="Child2",
            owner_human_id="human-1", subject="agent-child2",
            granted_scope=["get_weather", "read_file", "admin_reset_database"],
            parent_identity_id=parent.identity_id,
        )


def test_token_registry_resolves_to_correct_identity(tmp_path):
    conn = reset(tmp_path / "test.db")
    _seed_org_human(conn)

    identity = register_identity(
        conn, organization_id="org-1", agent_id="agent-a", agent_name="A",
        owner_human_id="human-1", subject="agent-a", granted_scope=["get_weather"],
    )
    token = issue_token(conn, agent_id="agent-a")

    registry = load_token_registry(conn)
    assert token in registry
    assert registry[token].agent_id == "agent-a"
    assert registry[token].identity_id == identity.identity_id
    assert registry[token].granted_scope == frozenset({"get_weather"})


def test_revoked_token_not_in_registry(tmp_path):
    conn = reset(tmp_path / "test.db")
    _seed_org_human(conn)
    register_identity(
        conn, organization_id="org-1", agent_id="agent-a", agent_name="A",
        owner_human_id="human-1", subject="agent-a", granted_scope=["get_weather"],
    )
    token = issue_token(conn, agent_id="agent-a")
    conn.execute("UPDATE credentials SET status = 'revoked' WHERE id = ?", (token,))
    conn.commit()

    registry = load_token_registry(conn)
    assert token not in registry
