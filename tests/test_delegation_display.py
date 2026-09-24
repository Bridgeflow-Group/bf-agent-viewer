"""F-028/T-015: nested sub-agent display. Per OQ-007's resolved decision,
every sub-agent spawn still gets a full, permanent Agent Identity (F-026
doesn't get lighter-weight) -- what changes is display: a sub-agent nests
under its parent's own agent detail view by default, rather than showing
up as a peer row on the top-level dashboard, and the detail view shows
the *full* delegation tree (every ancestor above, every descendant below),
not just the one-hop parent link agent_detail.html rendered before this.
"""
from __future__ import annotations

import pyotp
import pytest

starlette_testclient = pytest.importorskip("starlette.testclient")
TestClient = starlette_testclient.TestClient

from bf_agent_viewer.console import auth, build_console, queries
from bf_agent_viewer.db import reset
from bf_agent_viewer.identity import register_identity

PASSWORD = "correct horse battery staple"


def _seed_org_human(conn):
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute("INSERT INTO humans (id, organization_id, name) VALUES ('human-1', 'org-1', 'Owner')")


def _seed_three_level_chain(conn):
    """root -> mid -> leaf, a genuine multi-hop delegation chain, not just
    a single parent/child pair -- the case a one-hop-only view would get
    wrong."""
    _seed_org_human(conn)
    root = register_identity(
        conn, organization_id="org-1", agent_id="root", agent_name="Root",
        owner_human_id="human-1", subject="root", granted_scope=["a", "b", "c"],
    )
    mid = register_identity(
        conn, organization_id="org-1", agent_id="mid", agent_name="Mid",
        owner_human_id="human-1", subject="mid", granted_scope=["a", "b"],
        parent_identity_id=root.identity_id,
    )
    register_identity(
        conn, organization_id="org-1", agent_id="leaf", agent_name="Leaf",
        owner_human_id="human-1", subject="leaf", granted_scope=["a"],
        parent_identity_id=mid.identity_id,
    )


# -- queries.list_agents: nested by default -------------------------------

def test_list_agents_hides_sub_agents_by_default(tmp_path):
    conn = reset(tmp_path / "deleg1.db")
    _seed_three_level_chain(conn)

    names = {a.name for a in queries.list_agents(conn)}
    assert names == {"Root"}


def test_list_agents_include_sub_agents_opts_back_in(tmp_path):
    conn = reset(tmp_path / "deleg2.db")
    _seed_three_level_chain(conn)

    names = {a.name for a in queries.list_agents(conn, include_sub_agents=True)}
    assert names == {"Root", "Mid", "Leaf"}


def test_list_agents_does_not_hide_agents_with_no_identity_at_all(tmp_path):
    """A passively-discovered (F-027) or otherwise identity-less agent has
    no agent_identities row at all -- it must not be mistaken for a
    sub-agent and hidden."""
    conn = reset(tmp_path / "deleg3.db")
    _seed_org_human(conn)
    conn.execute(
        "INSERT INTO agents (id, organization_id, name, status) VALUES ('bare', 'org-1', 'Bare Agent', 'unclaimed')"
    )
    conn.commit()
    names = {a.name for a in queries.list_agents(conn)}
    assert "Bare Agent" in names


# -- queries.get_agent: full ancestor chain and descendant tree ----------

def test_get_agent_ancestor_chain_is_full_not_one_hop(tmp_path):
    conn = reset(tmp_path / "deleg4.db")
    _seed_three_level_chain(conn)

    leaf = queries.get_agent(conn, "leaf")
    assert leaf.ancestor_chain == [("root", "Root"), ("mid", "Mid")]
    # backward-compat one-hop field still points at the immediate parent
    assert leaf.parent_agent_id == "mid"


def test_get_agent_sub_agents_tree_is_full_not_one_hop(tmp_path):
    conn = reset(tmp_path / "deleg5.db")
    _seed_three_level_chain(conn)

    root = queries.get_agent(conn, "root")
    assert len(root.sub_agents) == 1
    mid_node = root.sub_agents[0]
    assert mid_node.id == "mid"
    assert len(mid_node.children) == 1
    assert mid_node.children[0].id == "leaf"
    assert mid_node.children[0].children == []


def test_get_agent_top_level_has_empty_ancestor_chain_and_real_sub_agents(tmp_path):
    conn = reset(tmp_path / "deleg6.db")
    _seed_three_level_chain(conn)

    root = queries.get_agent(conn, "root")
    assert root.ancestor_chain == []
    assert root.parent_agent_id is None
    assert [n.id for n in root.sub_agents] == ["mid"]


def test_get_agent_leaf_has_no_sub_agents(tmp_path):
    conn = reset(tmp_path / "deleg7.db")
    _seed_three_level_chain(conn)

    leaf = queries.get_agent(conn, "leaf")
    assert leaf.sub_agents == []


# -- real console pages ---------------------------------------------------

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
    conn = reset(tmp_path / "deleg_console.db", check_same_thread=False)
    _seed_three_level_chain(conn)
    conn.execute("UPDATE humans SET email = 'ada@example.com' WHERE id = 'human-1'")
    auth.provision_console_user(conn, human_id="human-1", password=PASSWORD, account_label="ada@example.com")
    conn.commit()
    app = build_console(conn)
    c = TestClient(app)
    _login(c, conn)
    return c, conn


def test_dashboard_shows_only_top_level_agent_as_its_own_row(client):
    c, _conn = client
    resp = c.get("/")
    assert resp.status_code == 200
    assert "Root" in resp.text
    # Mid and Leaf must not appear as their own dashboard rows -- exactly
    # zero occurrences of a link to their agent-detail page, since they
    # have no events of their own to also surface via the recent-activity
    # panel in this fixture (unlike test_console.py's fixture, which does
    # log an event for its sub-agent and would legitimately show a link
    # there too).
    assert 'href="/agents/mid"' not in resp.text
    assert 'href="/agents/leaf"' not in resp.text


def test_agent_detail_shows_full_nested_delegation_tree(client):
    c, _conn = client
    resp = c.get("/agents/root")
    assert resp.status_code == 200
    assert "Sub-agents" in resp.text
    # Both the direct child and the grandchild are shown, nested -- not
    # just the immediate one-hop child.
    assert 'href="/agents/mid"' in resp.text
    assert 'href="/agents/leaf"' in resp.text


def test_agent_detail_shows_full_ancestor_breadcrumb_not_one_hop(client):
    c, _conn = client
    resp = c.get("/agents/leaf")
    assert resp.status_code == 200
    # Both ancestors appear as links in the breadcrumb, not just the
    # immediate parent (Mid) -- Root, two hops up, must be reachable too.
    assert 'href="/agents/root"' in resp.text
    assert 'href="/agents/mid"' in resp.text


def test_agent_detail_top_level_shows_no_ancestor_breadcrumb(client):
    c, _conn = client
    resp = c.get("/agents/root")
    assert resp.status_code == 200
    assert "(top-level)" in resp.text
