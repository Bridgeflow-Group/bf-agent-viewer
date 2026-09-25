import pytest

from bf_agent_viewer.db import reset
from bf_agent_viewer.identity import (
    client_info_name,
    issue_token,
    load_token_registry,
    register_identity,
    renew_token,
    revoke_token,
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


# F-016/T-024: short-lived credential support -- issue_token's ttl_seconds,
# renew_token, revoke_token, and load_token_registry's new expiry filter.

def test_standing_token_has_no_expiry_and_is_unaffected(tmp_path):
    """v0.1.0 backward-compat: omitting ttl_seconds must behave exactly as
    before -- a standing token that never expires."""
    conn = reset(tmp_path / "test.db")
    _seed_org_human(conn)
    register_identity(
        conn, organization_id="org-1", agent_id="agent-a", agent_name="A",
        owner_human_id="human-1", subject="agent-a", granted_scope=["get_weather"],
    )
    token = issue_token(conn, agent_id="agent-a")
    expiry = conn.execute("SELECT expiry FROM credentials WHERE id = ?", (token,)).fetchone()[0]
    assert expiry is None
    assert token in load_token_registry(conn)


def test_short_lived_token_expires_and_drops_out_of_registry(tmp_path):
    conn = reset(tmp_path / "test.db")
    _seed_org_human(conn)
    register_identity(
        conn, organization_id="org-1", agent_id="agent-a", agent_name="A",
        owner_human_id="human-1", subject="agent-a", granted_scope=["get_weather"],
    )
    # Negative TTL -- already expired the instant it's issued, so the test
    # doesn't need a real sleep to observe the effect.
    token = issue_token(conn, agent_id="agent-a", ttl_seconds=-1)
    assert token not in load_token_registry(conn)


def test_short_lived_token_still_active_within_ttl(tmp_path):
    conn = reset(tmp_path / "test.db")
    _seed_org_human(conn)
    register_identity(
        conn, organization_id="org-1", agent_id="agent-a", agent_name="A",
        owner_human_id="human-1", subject="agent-a", granted_scope=["get_weather"],
    )
    token = issue_token(conn, agent_id="agent-a", ttl_seconds=3600)
    assert token in load_token_registry(conn)


def test_renew_token_extends_expiry(tmp_path):
    conn = reset(tmp_path / "test.db")
    _seed_org_human(conn)
    register_identity(
        conn, organization_id="org-1", agent_id="agent-a", agent_name="A",
        owner_human_id="human-1", subject="agent-a", granted_scope=["get_weather"],
    )
    token = issue_token(conn, agent_id="agent-a", ttl_seconds=1)
    before = conn.execute("SELECT expiry FROM credentials WHERE id = ?", (token,)).fetchone()[0]
    new_expiry = renew_token(conn, token, ttl_seconds=3600)
    after = conn.execute("SELECT expiry FROM credentials WHERE id = ?", (token,)).fetchone()[0]
    assert after == new_expiry
    assert after > before
    assert token in load_token_registry(conn)


def test_renew_token_refuses_an_already_expired_credential(tmp_path):
    """Renewal must not be able to resurrect a credential that's already
    lapsed -- that would defeat the entire point of a TTL."""
    conn = reset(tmp_path / "test.db")
    _seed_org_human(conn)
    register_identity(
        conn, organization_id="org-1", agent_id="agent-a", agent_name="A",
        owner_human_id="human-1", subject="agent-a", granted_scope=["get_weather"],
    )
    token = issue_token(conn, agent_id="agent-a", ttl_seconds=-1)
    with pytest.raises(ValueError):
        renew_token(conn, token, ttl_seconds=3600)


def test_renew_token_refuses_a_revoked_credential(tmp_path):
    conn = reset(tmp_path / "test.db")
    _seed_org_human(conn)
    register_identity(
        conn, organization_id="org-1", agent_id="agent-a", agent_name="A",
        owner_human_id="human-1", subject="agent-a", granted_scope=["get_weather"],
    )
    token = issue_token(conn, agent_id="agent-a", ttl_seconds=3600)
    revoke_token(conn, token)
    with pytest.raises(ValueError):
        renew_token(conn, token, ttl_seconds=3600)


def test_renew_token_refuses_a_nonexistent_token(tmp_path):
    conn = reset(tmp_path / "test.db")
    with pytest.raises(ValueError):
        renew_token(conn, "bfav_does-not-exist", ttl_seconds=3600)


def test_revoke_token_is_immediate_regardless_of_ttl(tmp_path):
    """revoke_token works on a standing (no-expiry) credential too -- TTL
    and revocation are independent mechanisms, not one built only for the
    other."""
    conn = reset(tmp_path / "test.db")
    _seed_org_human(conn)
    register_identity(
        conn, organization_id="org-1", agent_id="agent-a", agent_name="A",
        owner_human_id="human-1", subject="agent-a", granted_scope=["get_weather"],
    )
    token = issue_token(conn, agent_id="agent-a")  # no TTL
    assert token in load_token_registry(conn)
    revoke_token(conn, token)
    assert token not in load_token_registry(conn)
    status, revoked_at = conn.execute(
        "SELECT status, revoked_at FROM credentials WHERE id = ?", (token,)
    ).fetchone()
    assert status == "revoked"
    assert revoked_at is not None


def test_revoke_token_refuses_an_already_revoked_credential(tmp_path):
    """Not idempotent against a caller's mistake -- a second revoke of the
    same token should surface an error, not silently succeed."""
    conn = reset(tmp_path / "test.db")
    _seed_org_human(conn)
    register_identity(
        conn, organization_id="org-1", agent_id="agent-a", agent_name="A",
        owner_human_id="human-1", subject="agent-a", granted_scope=["get_weather"],
    )
    token = issue_token(conn, agent_id="agent-a")
    revoke_token(conn, token)
    with pytest.raises(ValueError):
        revoke_token(conn, token)


def test_revoke_token_refuses_a_nonexistent_token(tmp_path):
    conn = reset(tmp_path / "test.db")
    with pytest.raises(ValueError):
        revoke_token(conn, "bfav_does-not-exist")
