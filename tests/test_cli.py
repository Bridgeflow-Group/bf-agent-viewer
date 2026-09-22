"""Real CLI invocations (bf_agent_viewer.cli.main) against a real SQLite
file -- not mocks. Covers org/human bootstrap (T-009: the gap found while
building Docker Compose packaging -- there was no CLI path from a blank
database to a registered agent, since register/console-user both assume
an organization and human already exist) plus the gateway command's
argument wiring."""
from __future__ import annotations

import os

import pytest

from bf_agent_viewer.cli import main
from bf_agent_viewer.db import connect


def test_org_create(tmp_path):
    db = str(tmp_path / "cli.db")
    main(["org", "create", "--db", db, "--id", "org-1", "--name", "Acme"])
    conn = connect(db)
    row = conn.execute("SELECT name FROM organizations WHERE id = 'org-1'").fetchone()
    assert row == ("Acme",)


def test_human_create_requires_existing_org(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    with pytest.raises(SystemExit):
        main(["human", "create", "--db", db, "--org", "does-not-exist", "--id", "human-1", "--name", "Ada"])
    assert "no organization" in capsys.readouterr().out.lower()


def test_human_create(tmp_path):
    db = str(tmp_path / "cli.db")
    main(["org", "create", "--db", db, "--id", "org-1", "--name", "Acme"])
    main(["human", "create", "--db", db, "--org", "org-1", "--id", "human-1", "--name", "Ada", "--email", "ada@example.com"])
    conn = connect(db)
    row = conn.execute("SELECT organization_id, name, email FROM humans WHERE id = 'human-1'").fetchone()
    assert row == ("org-1", "Ada", "ada@example.com")


def test_full_bootstrap_to_registered_agent(tmp_path, capsys):
    """The actual end-to-end path a fresh install needs: org -> human ->
    register (agent + token). Nothing here existed as a CLI path before
    org/human create were added -- this is the test that would have
    caught that gap."""
    db = str(tmp_path / "cli.db")
    main(["org", "create", "--db", db, "--id", "org-1", "--name", "Acme"])
    main(["human", "create", "--db", db, "--org", "org-1", "--id", "human-1", "--name", "Ada"])
    main([
        "register", "--db", db, "--org", "org-1", "--agent-id", "agent-1", "--name", "Agent One",
        "--owner", "human-1", "--scope", "get_weather",
    ])
    out = capsys.readouterr().out
    assert "registered agent-1" in out
    assert "token: bfav_" in out

    conn = connect(db)
    row = conn.execute("SELECT owner_id FROM agents WHERE id = 'agent-1'").fetchone()
    assert row == ("human-1",)


def test_env_var_fallback_for_db_and_org(tmp_path, monkeypatch):
    db = str(tmp_path / "cli.db")
    monkeypatch.setenv("BF_DB", db)
    monkeypatch.setenv("BF_ORG", "org-1")
    main(["org", "create", "--db", db, "--id", "org-1", "--name", "Acme"])
    # No --db/--org passed here -- should be picked up from the env vars.
    main(["human", "create", "--id", "human-1", "--name", "Ada"])
    conn = connect(db)
    row = conn.execute("SELECT organization_id FROM humans WHERE id = 'human-1'").fetchone()
    assert row == ("org-1",)


def test_gateway_command_wires_build_gateway_correctly(tmp_path, monkeypatch):
    """Doesn't actually run the gateway (gateway.run() blocks serving
    HTTP forever) -- asserts cmd_gateway builds it with the right args
    from CLI flags/env vars, which is the part that's easy to get wrong
    silently (e.g. write_tools parsing, prefer_container wiring)."""
    db = str(tmp_path / "cli.db")
    connect(db)  # just needs to exist

    calls = {}

    class FakeGateway:
        def run(self, **kwargs):
            calls["run_kwargs"] = kwargs

    def fake_build_gateway(conn, **kwargs):
        calls["build_kwargs"] = kwargs
        return FakeGateway(), object()

    import bf_agent_viewer.gateway as gateway_module
    monkeypatch.setattr(gateway_module, "build_gateway", fake_build_gateway)

    main([
        "gateway", "--db", db, "--org", "org-1", "--backend-script", "/tmp/backend.py",
        "--write-tools", "send_email, delete_record", "--host", "0.0.0.0", "--port", "9999",
        "--no-container",
    ])

    assert calls["build_kwargs"]["organization_id"] == "org-1"
    assert calls["build_kwargs"]["backend_script"] == "/tmp/backend.py"
    assert calls["build_kwargs"]["write_tools"] == {"send_email", "delete_record"}
    assert calls["build_kwargs"]["prefer_container"] is False
    assert calls["run_kwargs"]["host"] == "0.0.0.0"
    assert calls["run_kwargs"]["port"] == 9999
