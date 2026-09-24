"""F-019/T-017: configurable log retention -- a retention-window setting
plus a prune job for the event store.

The interesting part isn't "delete old rows" -- it's that the event
store's tamper-evident hash chain (OQ-012) chains each row to the
previous row's hash starting from a fixed "GENESIS" sentinel, so deleting
the true first rows would otherwise make verify_chain() report a false
tamper alarm on the very next check. These tests cover: the prune window
itself (what gets kept vs removed, including a clock-skew edge case),
that the hash chain still verifies clean afterward, that new events
logged after a full prune still chain correctly, the CLI path against a
real file-backed database, and a direct demonstration of the bug this
would be without the checkpoint fix.
"""
from __future__ import annotations

import subprocess
import sys
import time
from unittest.mock import patch

from bf_agent_viewer.cli import main as cli_main
from bf_agent_viewer.db import reset
from bf_agent_viewer.events import last_hash, log_event, verify_chain
from bf_agent_viewer.retention import DEFAULT_RETENTION_DAYS, prune_events


def _seed_org(conn):
    conn.execute("INSERT INTO organizations (id, name) VALUES ('org-1', 'Org')")
    conn.execute(
        "INSERT INTO agents (id, organization_id, name, owner_id) "
        "VALUES ('agent-1', 'org-1', 'Agent', 'human-1')"
    )


def _log(conn, prev, *, occurred_at, tool_id="tool-1"):
    """Logs one event stamped with a given occurred_at (the real
    log_event() always stamps "now" -- tests need control over the
    timestamp to exercise retention windows without sleeping). Patches
    the same time.gmtime() call log_event() itself uses, so the inserted
    content_hash is computed from the intended occurred_at directly
    rather than being patched onto the row after the fact, which would
    desync the stored hash from the payload it was actually derived from
    (occurred_at is part of that payload -- see events/log.py)."""
    struct = time.strptime(occurred_at, "%Y-%m-%dT%H:%M:%SZ")
    with patch("bf_agent_viewer.events.log.time.gmtime", return_value=struct):
        event_id, new_prev = log_event(
            conn, organization_id="org-1", agent_id="agent-1", session_id=None,
            actor_human_id=None, event_type="tool.called", action="read",
            tool_id=tool_id, metadata={}, prev_hash=prev,
        )
    conn.commit()
    return event_id, new_prev


def test_prune_default_is_six_months():
    assert DEFAULT_RETENTION_DAYS == 180.0


def test_prune_removes_only_events_older_than_window(tmp_path):
    conn = reset(tmp_path / "test.db")
    _seed_org(conn)
    prev = last_hash(conn)
    old_id, prev = _log(conn, prev, occurred_at="2026-01-01T00:00:00Z")
    kept_id, prev = _log(conn, prev, occurred_at="2026-09-01T00:00:00Z")

    result = prune_events(conn, retention_days=180, now="2026-09-24T00:00:00Z")

    assert result.events_pruned == 1
    remaining = [r[0] for r in conn.execute("SELECT id FROM events").fetchall()]
    assert remaining == [kept_id]
    assert conn.execute(
        "SELECT pruned_through_event_id FROM retention_checkpoints"
    ).fetchone()[0] == old_id


def test_prune_no_op_when_nothing_eligible(tmp_path):
    conn = reset(tmp_path / "test.db")
    _seed_org(conn)
    prev = last_hash(conn)
    _log(conn, prev, occurred_at="2026-09-20T00:00:00Z")

    result = prune_events(conn, retention_days=180, now="2026-09-24T00:00:00Z")

    assert result.events_pruned == 0
    assert result.chain_tip_hash is None
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM retention_checkpoints").fetchone()[0] == 0


def test_prune_stops_at_first_non_stale_row_even_with_clock_skew(tmp_path):
    """A later-inserted (higher rowid) event with an earlier timestamp
    than an event ahead of it must not cause pruning to skip past a
    fresher row to reach it -- pruning only ever removes a contiguous
    rowid prefix."""
    conn = reset(tmp_path / "test.db")
    _seed_org(conn)
    prev = last_hash(conn)
    # rowid 1: genuinely old.
    id1, prev = _log(conn, prev, occurred_at="2026-01-01T00:00:00Z")
    # rowid 2: fresh -- NOT eligible, even though inserted before id3.
    id2, prev = _log(conn, prev, occurred_at="2026-09-20T00:00:00Z")
    # rowid 3: clock-skewed to look old, but sits after a fresh row.
    id3, prev = _log(conn, prev, occurred_at="2026-01-02T00:00:00Z")

    result = prune_events(conn, retention_days=180, now="2026-09-24T00:00:00Z")

    # Only id1 (the contiguous eligible prefix) is removed; id3 is left
    # for a future run even though its own timestamp looks eligible.
    assert result.events_pruned == 1
    remaining = {r[0] for r in conn.execute("SELECT id FROM events").fetchall()}
    assert remaining == {id2, id3}


def test_chain_still_verifies_clean_after_prune(tmp_path):
    conn = reset(tmp_path / "test.db")
    _seed_org(conn)
    prev = last_hash(conn)
    for i in range(5):
        _, prev = _log(conn, prev, occurred_at=f"2026-01-0{i + 1}T00:00:00Z", tool_id=f"old-{i}")
    for i in range(5):
        _, prev = _log(conn, prev, occurred_at=f"2026-09-1{i}T00:00:00Z", tool_id=f"new-{i}")

    result = prune_events(conn, retention_days=180, now="2026-09-24T00:00:00Z")
    assert result.events_pruned == 5

    ok, bad_id = verify_chain(conn)
    assert ok
    assert bad_id is None


def test_new_events_after_full_prune_still_chain_correctly(tmp_path):
    """Edge case: retention short enough (or a run late enough) that
    every existing event gets pruned. The next event logged must chain
    from the checkpoint, not from GENESIS -- otherwise verify_chain()
    (which correctly starts from the checkpoint) would flag it as
    tampered even though nothing was actually tampered with."""
    conn = reset(tmp_path / "test.db")
    _seed_org(conn)
    prev = last_hash(conn)
    for i in range(3):
        _, prev = _log(conn, prev, occurred_at=f"2026-01-0{i + 1}T00:00:00Z", tool_id=f"old-{i}")

    result = prune_events(conn, retention_days=180, now="2026-09-24T00:00:00Z")
    assert result.events_pruned == 3
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0

    # last_hash() must fall back to the checkpoint, not GENESIS.
    new_prev = last_hash(conn)
    assert new_prev == result.chain_tip_hash

    log_event(
        conn, organization_id="org-1", agent_id="agent-1", session_id=None,
        actor_human_id=None, event_type="tool.called", action="read",
        tool_id="post-prune", metadata={}, prev_hash=new_prev,
    )
    conn.commit()

    ok, bad_id = verify_chain(conn)
    assert ok
    assert bad_id is None


def test_without_checkpoint_fallback_verification_would_false_positive(tmp_path):
    """Not a test of new behavior -- a direct demonstration of the bug the
    checkpoint mechanism fixes, so a future change can't silently drop it.
    Replays the pre-fix algorithm (always start from GENESIS, ignore any
    checkpoint) against a pruned chain and confirms it wrongly reports
    tampering, in contrast to the real verify_chain() on the same data."""
    import hashlib
    import json

    conn = reset(tmp_path / "test.db")
    _seed_org(conn)
    prev = last_hash(conn)
    for i in range(3):
        _, prev = _log(conn, prev, occurred_at=f"2026-01-0{i + 1}T00:00:00Z", tool_id=f"old-{i}")
    for i in range(3):
        _, prev = _log(conn, prev, occurred_at=f"2026-09-1{i}T00:00:00Z", tool_id=f"new-{i}")

    prune_events(conn, retention_days=180, now="2026-09-24T00:00:00Z")

    # The real, fixed implementation: clean.
    ok, _ = verify_chain(conn)
    assert ok

    # The naive pre-fix algorithm: always assumes GENESIS.
    rows = conn.execute(
        "SELECT id, occurred_at, agent_id, event_type, action, resource_id, content_hash "
        "FROM events ORDER BY created_at, rowid"
    ).fetchall()
    naive_prev = "GENESIS"
    naive_ok = True
    for event_id, occurred_at, agent_id, event_type, action, resource_id, stored_hash in rows:
        payload = {
            "id": event_id, "occurred_at": occurred_at, "agent_id": agent_id,
            "event_type": event_type, "action": action, "resource_id": resource_id,
        }
        expected = hashlib.sha256(
            (naive_prev + json.dumps(payload, sort_keys=True)).encode()
        ).hexdigest()
        if expected != stored_hash:
            naive_ok = False
            break
        naive_prev = stored_hash
    assert naive_ok is False, (
        "the naive GENESIS-only algorithm should falsely flag a pruned-but-untampered "
        "chain -- if this now passes, the checkpoint fix may have regressed"
    )


def test_repeated_prune_runs_advance_the_checkpoint(tmp_path):
    conn = reset(tmp_path / "test.db")
    _seed_org(conn)
    prev = last_hash(conn)
    id1, prev = _log(conn, prev, occurred_at="2026-01-01T00:00:00Z")
    id2, prev = _log(conn, prev, occurred_at="2026-03-01T00:00:00Z")
    id3, prev = _log(conn, prev, occurred_at="2026-09-01T00:00:00Z")

    r1 = prune_events(conn, retention_days=180, now="2026-07-15T00:00:00Z")
    assert r1.events_pruned == 1
    r2 = prune_events(conn, retention_days=180, now="2026-09-24T00:00:00Z")
    assert r2.events_pruned == 1

    remaining = [r[0] for r in conn.execute("SELECT id FROM events").fetchall()]
    assert remaining == [id3]
    assert conn.execute("SELECT COUNT(*) FROM retention_checkpoints").fetchone()[0] == 2

    ok, bad_id = verify_chain(conn)
    assert ok
    assert bad_id is None


def test_cli_retention_prune_real_file_backed_db(tmp_path, capsys):
    db_path = str(tmp_path / "cli.db")
    conn = reset(db_path)
    _seed_org(conn)
    prev = last_hash(conn)
    old_id, prev = _log(conn, prev, occurred_at="2026-01-01T00:00:00Z")
    kept_id, prev = _log(conn, prev, occurred_at="2026-09-20T00:00:00Z")
    conn.close()

    exit_code = cli_main([
        "retention", "prune", "--db", db_path, "--retention-days", "180",
    ])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "pruned 1 event" in out
    assert "chain checkpoint recorded" in out


def test_cli_retention_prune_subprocess_real_invocation(tmp_path):
    """A genuine end-to-end smoke test: real subprocess, real SQLite file
    on disk, not an in-process call. Mirrors the pattern used for
    `export events` in test_export.py."""
    db_path = str(tmp_path / "smoke.db")
    conn = reset(db_path)
    _seed_org(conn)
    prev = last_hash(conn)
    _log(conn, prev, occurred_at="2026-01-01T00:00:00Z")
    conn.close()

    proc = subprocess.run(
        [sys.executable, "-m", "bf_agent_viewer.cli", "retention", "prune",
         "--db", db_path, "--retention-days", "180"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "pruned 1 event" in proc.stdout

    import sqlite3 as _sqlite3
    conn2 = _sqlite3.connect(db_path)
    remaining = conn2.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert remaining == 0
    checkpoints = conn2.execute("SELECT COUNT(*) FROM retention_checkpoints").fetchone()[0]
    assert checkpoints == 1


def test_cli_retention_prune_defaults_to_six_months(tmp_path, capsys):
    """No --retention-days given -- the default (compliance-safe ~6
    months) applies, matching CLI.md's documented default."""
    db_path = str(tmp_path / "default.db")
    conn = reset(db_path)
    _seed_org(conn)
    prev = last_hash(conn)
    _log(conn, prev, occurred_at="2020-01-01T00:00:00Z")  # far in the past
    conn.close()

    exit_code = cli_main(["retention", "prune", "--db", db_path])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "pruned 1 event" in out
    assert "retention 180" in out
