"""F-027/T-012: passive-discovery registration path. An unrecognized
caller gets a real, visible-but-unowned `agents` row instead of the old
fixed placeholder that no other part of the product could see -- and
claiming that row is a real state transition (owner + fresh identity +
credential), not just filling in an owner_id."""
from __future__ import annotations

import pytest

from bf_agent_viewer.console import queries
from bf_agent_viewer.db import reset
from bf_agent_viewer.identity import claim_discovered_agent, issue_token, register_identity
from bf_agent_viewer.identity.discovery import (
    UNNAMED_AGENT_ID,
    discover_agent,
    discovery_agent_id,
)


def _seed_org_human(conn):
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('human-1', 'org-1', 'Owner')")


# -- discovery_agent_id --------------------------------------------------

def test_discovery_agent_id_keyed_by_asserted_name():
    assert discovery_agent_id({"name": "acme-bot", "version": "1.0"}) == "discovered-acme-bot"


def test_discovery_agent_id_slugifies_odd_names():
    assert discovery_agent_id({"name": "Acme Bot v2!!"}) == "discovered-acme-bot-v2"


def test_discovery_agent_id_falls_back_to_shared_unnamed_id():
    assert discovery_agent_id(None) == UNNAMED_AGENT_ID
    assert discovery_agent_id({"version": "1.0"}) == UNNAMED_AGENT_ID


# -- discover_agent (real DB) --------------------------------------------

def test_discover_agent_creates_a_real_visible_row(tmp_path):
    conn = reset(tmp_path / "disc1.db")
    _seed_org_human(conn)

    agent_id, first = discover_agent(
        conn, organization_id="org-1", client_info={"name": "some-bot"},
    )
    assert first is True
    assert agent_id == "discovered-some-bot"

    row = conn.execute(
        "SELECT owner_id, status FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()
    assert row == (None, "unclaimed")

    # It's a real row -- the dashboard's own query (not a special-case
    # path) picks it up, owner shown as unset.
    agents = {a.id: a for a in queries.list_agents(conn)}
    assert agents[agent_id].owner_name is None
    assert agents[agent_id].status == "unclaimed"


def test_discover_agent_is_idempotent_and_reports_first_sighting_once(tmp_path):
    conn = reset(tmp_path / "disc2.db")
    _seed_org_human(conn)

    _id1, first1 = discover_agent(conn, organization_id="org-1", client_info={"name": "bot-a"})
    _id2, first2 = discover_agent(conn, organization_id="org-1", client_info={"name": "bot-a"})
    assert first1 is True
    assert first2 is False

    count = conn.execute("SELECT COUNT(*) FROM agents WHERE id = 'discovered-bot-a'").fetchone()[0]
    assert count == 1


def test_discover_agent_distinguishes_different_asserted_names(tmp_path):
    conn = reset(tmp_path / "disc3.db")
    _seed_org_human(conn)

    id_a, _ = discover_agent(conn, organization_id="org-1", client_info={"name": "bot-a"})
    id_b, _ = discover_agent(conn, organization_id="org-1", client_info={"name": "bot-b"})
    assert id_a != id_b


def test_discover_agent_collapses_nameless_callers_into_one_shared_row(tmp_path):
    """No honest way to split two callers that assert nothing at all --
    both land on the same shared UNNAMED_AGENT_ID row."""
    conn = reset(tmp_path / "disc4.db")
    _seed_org_human(conn)

    id_1, first1 = discover_agent(conn, organization_id="org-1", client_info=None)
    id_2, first2 = discover_agent(conn, organization_id="org-1", client_info=None)
    assert id_1 == id_2 == UNNAMED_AGENT_ID
    assert (first1, first2) == (True, False)


# -- claim_discovered_agent (real DB) ------------------------------------

def test_claim_discovered_agent_assigns_owner_and_real_identity(tmp_path):
    conn = reset(tmp_path / "claim1.db")
    _seed_org_human(conn)
    agent_id, _ = discover_agent(conn, organization_id="org-1", client_info={"name": "acme-bot"})

    identity = claim_discovered_agent(
        conn, organization_id="org-1", agent_id=agent_id, owner_human_id="human-1",
        granted_scope=["get_weather"],
    )
    assert identity.agent_id == agent_id
    assert identity.granted_scope == frozenset({"get_weather"})

    row = conn.execute("SELECT owner_id, status FROM agents WHERE id = ?", (agent_id,)).fetchone()
    assert row == ("human-1", "active")

    # Claiming is a real identity, not the discovery clientInfo carried
    # forward -- a token issued against it resolves with the granted scope.
    token = issue_token(conn, agent_id=agent_id)
    from bf_agent_viewer.identity import load_token_registry
    registry = load_token_registry(conn)
    assert registry[token].agent_id == agent_id
    assert registry[token].granted_scope == frozenset({"get_weather"})


def test_claim_refuses_unknown_agent_id(tmp_path):
    conn = reset(tmp_path / "claim2.db")
    _seed_org_human(conn)
    with pytest.raises(ValueError):
        claim_discovered_agent(
            conn, organization_id="org-1", agent_id="discovered-nope",
            owner_human_id="human-1", granted_scope=["get_weather"],
        )


def test_claim_refuses_an_already_active_agent(tmp_path):
    """An explicitly registered agent (status='active') was never in the
    discovered/unclaimed state -- claim must not silently re-own it."""
    conn = reset(tmp_path / "claim3.db")
    _seed_org_human(conn)
    register_identity(
        conn, organization_id="org-1", agent_id="agent-1", agent_name="Agent",
        owner_human_id="human-1", subject="agent-1", granted_scope=["get_weather"],
    )
    with pytest.raises(ValueError):
        claim_discovered_agent(
            conn, organization_id="org-1", agent_id="agent-1",
            owner_human_id="human-1", granted_scope=["get_weather"],
        )


def test_claim_refuses_double_claiming(tmp_path):
    conn = reset(tmp_path / "claim4.db")
    _seed_org_human(conn)
    agent_id, _ = discover_agent(conn, organization_id="org-1", client_info={"name": "acme-bot"})
    claim_discovered_agent(
        conn, organization_id="org-1", agent_id=agent_id, owner_human_id="human-1",
        granted_scope=["get_weather"],
    )
    with pytest.raises(ValueError):
        claim_discovered_agent(
            conn, organization_id="org-1", agent_id=agent_id, owner_human_id="human-1",
            granted_scope=["get_weather"],
        )


# -- schema migration -----------------------------------------------------

def test_agents_owner_id_is_nullable_on_a_fresh_database(tmp_path):
    conn = reset(tmp_path / "schema1.db")
    columns = conn.execute("PRAGMA table_info(agents)").fetchall()
    owner_col = next(c for c in columns if c[1] == "owner_id")
    assert owner_col[3] == 0  # notnull flag off


def test_migration_loosens_an_existing_not_null_owner_column(tmp_path):
    """Simulate a database created before T-012, with the old NOT NULL
    owner_id -- reconnecting must rebuild it to nullable without losing
    existing rows, and must be a no-op on the next connect() after that."""
    import sqlite3

    from bf_agent_viewer.db import connect
    from bf_agent_viewer.db.connection import _migrate_agents_owner_nullable

    db_path = tmp_path / "old_shape.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE organizations (id TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE humans (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, name TEXT NOT NULL);
        CREATE TABLE agents (
            id                  TEXT PRIMARY KEY,
            organization_id     TEXT NOT NULL,
            name                TEXT NOT NULL,
            owner_id            TEXT NOT NULL,
            technical_owner_id  TEXT,
            environment         TEXT,
            status              TEXT NOT NULL DEFAULT 'unclaimed',
            autonomy_tier       TEXT,
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO organizations VALUES ('org-1', 'Org');
        INSERT INTO humans VALUES ('human-1', 'org-1', 'Owner');
        INSERT INTO agents (id, organization_id, name, owner_id, status)
            VALUES ('agent-1', 'org-1', 'Agent', 'human-1', 'active');
        """
    )
    conn.commit()
    conn.close()

    conn = connect(db_path)  # runs the migration as part of a normal connect()
    columns = conn.execute("PRAGMA table_info(agents)").fetchall()
    owner_col = next(c for c in columns if c[1] == "owner_id")
    assert owner_col[3] == 0
    # The pre-existing row survived the rebuild intact.
    row = conn.execute("SELECT id, owner_id, status FROM agents WHERE id = 'agent-1'").fetchone()
    assert row == ("agent-1", "human-1", "active")

    # A brand-new discovered row (owner_id NULL) now inserts cleanly.
    agent_id, _ = discover_agent(conn, organization_id="org-1", client_info={"name": "new-bot"})
    assert conn.execute(
        "SELECT owner_id FROM agents WHERE id = ?", (agent_id,)
    ).fetchone() == (None,)

    conn.close()
    # Reconnecting again must be a no-op (idempotent migration).
    conn2 = connect(db_path)
    assert conn2.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 2
