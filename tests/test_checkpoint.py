"""F-014/T-040: the external chain-tip anchor -- periodic signed
checkpoints written to a second local file, separate from the main
SQLite database (OQ-012's addendum). The interesting part isn't just
"write a file" -- it's that the checkpoint has to be independently
verifiable (signed with a key the database never touches) and has to
actually catch a database-level tamper that the in-DB hash chain alone
(events.verify_chain, covered by test_events.py) can't, since a fully
regenerated fake-but-internally-consistent history would pass that check
on its own.
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys

import pytest

from bf_agent_viewer.cli import main as cli_main
from bf_agent_viewer.db import reset
from bf_agent_viewer.events import log_event
from bf_agent_viewer.integrity import (
    default_checkpoint_path,
    default_signing_key_path,
    load_or_create_signing_key,
    verify_against_events,
    verify_checkpoint_file,
    write_checkpoint,
)


def _strip_append_only(path):
    """Tests that tamper with a checkpoint file directly need to undo
    write_checkpoint()'s own `chattr +a` first -- on a filesystem that
    actually supports it (this sandbox's does), the immutable-append
    flag genuinely blocks a plain overwrite, which is the point of it.
    Modeling "an attacker who can remove the flag" (root) here, since
    the interesting guarantee under test in these cases is the
    cryptographic one, not the OS-level deterrent -- that deterrent's
    own success is incidentally confirmed by needing this at all."""
    subprocess.run(["chattr", "-a", str(path)], capture_output=True, check=False)


def _seed_org(conn):
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute(
        "INSERT INTO agents (id, organization_id, name, owner_id) "
        "VALUES ('agent-1', 'org-1', 'Agent', NULL)"
    )


def _log(conn, prev, tool_id="tool-1"):
    _, new_prev = log_event(
        conn, organization_id="org-1", agent_id="agent-1", session_id=None,
        actor_human_id=None, event_type="tool.called", action="read",
        tool_id=tool_id, metadata={}, prev_hash=prev,
    )
    conn.commit()
    return new_prev


def test_first_checkpoint_generates_a_signing_key_and_writes_seq_zero(tmp_path):
    conn = reset(tmp_path / "cp1.db")
    _seed_org(conn)
    prev = _log(conn, "GENESIS")

    checkpoint_path = tmp_path / "cp1.checkpoints.jsonl"
    key_path = tmp_path / "cp1.signing-key"
    assert not key_path.exists()

    checkpoint = write_checkpoint(conn, checkpoint_path=checkpoint_path, key_path=key_path)

    assert checkpoint.seq == 0
    assert checkpoint.chain_tip_hash == prev
    assert checkpoint.event_count == 1
    assert key_path.exists()
    assert key_path.with_name(key_path.name + ".pub").exists()
    # Private key file must not be group/world readable.
    assert oct(key_path.stat().st_mode)[-3:] == "600"


def test_reusing_the_same_key_path_reuses_the_same_key(tmp_path):
    key_path = tmp_path / "reuse.key"
    key1 = load_or_create_signing_key(key_path)
    key2 = load_or_create_signing_key(key_path)
    assert key1.private_bytes_raw() == key2.private_bytes_raw()


def test_successive_checkpoints_chain_and_increment_seq(tmp_path):
    conn = reset(tmp_path / "cp2.db")
    _seed_org(conn)
    checkpoint_path = tmp_path / "cp2.checkpoints.jsonl"
    key_path = tmp_path / "cp2.signing-key"

    prev = "GENESIS"
    for i in range(3):
        prev = _log(conn, prev, tool_id=f"tool-{i}")
        cp = write_checkpoint(conn, checkpoint_path=checkpoint_path, key_path=key_path)
        assert cp.seq == i
        assert cp.event_count == i + 1

    lines = checkpoint_path.read_text().strip().splitlines()
    assert len(lines) == 3
    entries = [json.loads(l) for l in lines]
    assert entries[1]["prev_checkpoint_hash"] == entries[0]["checkpoint_hash"]
    assert entries[2]["prev_checkpoint_hash"] == entries[1]["checkpoint_hash"]

    ok, err = verify_checkpoint_file(checkpoint_path, key_path)
    assert ok, err


def test_verify_against_events_passes_on_an_untampered_database(tmp_path):
    conn = reset(tmp_path / "cp3.db")
    _seed_org(conn)
    prev = "GENESIS"
    for i in range(5):
        prev = _log(conn, prev, tool_id=f"tool-{i}")

    checkpoint_path = tmp_path / "cp3.checkpoints.jsonl"
    key_path = tmp_path / "cp3.signing-key"
    write_checkpoint(conn, checkpoint_path=checkpoint_path, key_path=key_path)

    ok, detail = verify_against_events(conn, checkpoint_path=checkpoint_path, key_path=key_path)
    assert ok, detail


def test_verify_against_events_catches_a_regenerated_fake_history(tmp_path):
    """The actual point of this whole feature: an attacker with write
    access to the SQLite file alone regenerates a completely fresh,
    internally-consistent hash chain from GENESIS (which would pass
    events.verify_chain() on its own -- see test_events.py) but can't
    reproduce the earlier signed checkpoint's tip, since the signing key
    was never in the database to begin with."""
    conn = reset(tmp_path / "cp4.db")
    _seed_org(conn)
    prev = "GENESIS"
    for i in range(4):
        prev = _log(conn, prev, tool_id=f"tool-{i}")

    checkpoint_path = tmp_path / "cp4.checkpoints.jsonl"
    key_path = tmp_path / "cp4.signing-key"
    write_checkpoint(conn, checkpoint_path=checkpoint_path, key_path=key_path)

    # Simulate a database-level compromise: wipe the real event history
    # and regenerate a fresh, self-consistent fake chain from GENESIS.
    conn.execute("DELETE FROM events")
    conn.commit()
    fake_prev = "GENESIS"
    for i in range(4):
        fake_prev = _log(conn, fake_prev, tool_id=f"forged-{i}")

    from bf_agent_viewer.events import verify_chain
    chain_ok, _ = verify_chain(conn)
    assert chain_ok, "sanity check: the forged chain must be internally self-consistent"

    ok, detail = verify_against_events(conn, checkpoint_path=checkpoint_path, key_path=key_path)
    assert not ok
    assert "does not appear anywhere in the current event table" in detail


def test_verify_checkpoint_file_catches_a_forged_signature(tmp_path):
    """A checkpoint file entry edited directly (not through write_checkpoint)
    -- e.g. an attacker who can write to the checkpoint file but doesn't
    have the private signing key -- must fail verification."""
    conn = reset(tmp_path / "cp5.db")
    _seed_org(conn)
    _log(conn, "GENESIS")

    checkpoint_path = tmp_path / "cp5.checkpoints.jsonl"
    key_path = tmp_path / "cp5.signing-key"
    write_checkpoint(conn, checkpoint_path=checkpoint_path, key_path=key_path)

    entry = json.loads(checkpoint_path.read_text().strip())
    entry["chain_tip_hash"] = "0" * 64  # tampered, signature no longer matches
    _strip_append_only(checkpoint_path)
    checkpoint_path.write_text(json.dumps(entry) + "\n")

    ok, err = verify_checkpoint_file(checkpoint_path, key_path)
    assert not ok
    assert "content hash" in err or "checkpoint_hash" in err


def test_verify_checkpoint_file_catches_a_deleted_entry(tmp_path):
    """Removing an entry from the middle of the checkpoint file breaks
    the prev_checkpoint_hash chain between the surviving entries, even
    though each surviving entry's own signature is still individually
    valid."""
    conn = reset(tmp_path / "cp6.db")
    _seed_org(conn)
    checkpoint_path = tmp_path / "cp6.checkpoints.jsonl"
    key_path = tmp_path / "cp6.signing-key"

    prev = "GENESIS"
    for i in range(3):
        prev = _log(conn, prev, tool_id=f"tool-{i}")
        write_checkpoint(conn, checkpoint_path=checkpoint_path, key_path=key_path)

    lines = checkpoint_path.read_text().strip().splitlines()
    assert len(lines) == 3
    # Drop the middle entry.
    _strip_append_only(checkpoint_path)
    checkpoint_path.write_text(lines[0] + "\n" + lines[2] + "\n")

    ok, err = verify_checkpoint_file(checkpoint_path, key_path)
    assert not ok
    assert "does not match the preceding entry" in err


def test_cli_checkpoint_write_then_verify_round_trips(tmp_path):
    db_path = str(tmp_path / "cli_cp.db")
    cli_main(["org", "create", "--db", db_path, "--id", "org-1", "--name", "Org"])
    cli_main(["checkpoint", "write", "--db", db_path])

    checkpoint_path = default_checkpoint_path(db_path)
    key_path = default_signing_key_path(db_path)
    assert checkpoint_path.exists()
    assert key_path.exists()

    # No SystemExit means verify succeeded.
    cli_main(["checkpoint", "verify", "--db", db_path])


def test_cli_checkpoint_verify_exits_nonzero_on_tamper(tmp_path, capsys):
    db_path = str(tmp_path / "cli_cp2.db")
    cli_main(["org", "create", "--db", db_path, "--id", "org-1", "--name", "Org"])

    conn = reset(db_path)
    _seed_org(conn)
    _log(conn, "GENESIS")

    cli_main(["checkpoint", "write", "--db", db_path])

    checkpoint_path = default_checkpoint_path(db_path)
    entry = json.loads(checkpoint_path.read_text().strip())
    entry["signature"] = base64.b64encode(b"not-a-real-signature").decode()
    _strip_append_only(checkpoint_path)
    checkpoint_path.write_text(json.dumps(entry) + "\n")

    with pytest.raises(SystemExit) as exc_info:
        cli_main(["checkpoint", "verify", "--db", db_path])
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "TAMPER DETECTED" in out


def test_real_subprocess_cli_writes_a_checkpoint_file(tmp_path):
    """One real end-to-end check via an actual subprocess invocation
    (not just calling cli.main() in-process) -- confirms the installed
    console-script-equivalent path (`python -m bf_agent_viewer.cli`)
    works too, matching test_retention.py's own real-subprocess check."""
    db_path = str(tmp_path / "subproc.db")
    subprocess.run(
        [sys.executable, "-m", "bf_agent_viewer.cli", "org", "create",
         "--db", db_path, "--id", "org-1", "--name", "Org"],
        check=True, capture_output=True,
    )
    result = subprocess.run(
        [sys.executable, "-m", "bf_agent_viewer.cli", "checkpoint", "write", "--db", db_path],
        check=True, capture_output=True, text=True,
    )
    assert "checkpoint seq 0 written" in result.stdout
    assert (tmp_path / "subproc.db.checkpoints.jsonl").exists()
