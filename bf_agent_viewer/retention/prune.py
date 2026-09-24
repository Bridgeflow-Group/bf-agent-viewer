"""Configurable log retention (F-019/T-017): a retention-window setting
plus a prune job for the event store.

Gap surfaced by regulatory-requirements.docx REQ-002 (EU AI Act Art.
19/26 minimum retention): a self-hosted event store that just grows
forever isn't actually compliant with a regime that expects logs to be
*disposed of* past some point too, and cheap to build into the schema
now versus expensive to retrofit once real customer data is sitting in
it. DEFAULT_RETENTION_DAYS below is the compliance-safe default the
feature description calls for (~6 months) -- per the License Tier
recorded in features.xlsx, per-organization/extended retention windows
are a possible later Paid enhancement; v0.1.0 Free ships one operator-
configurable default window for the whole install, in the same spirit as
T-016/F-034's CSV-only-for-now scoping of OQ-011.

Deleting old rows from `events` is not simply
`DELETE FROM events WHERE occurred_at < cutoff` -- events/log.py's
tamper-evident hash chain (OQ-012) chains each row's content_hash to the
*previous* row's hash, starting from the literal string "GENESIS" for
the very first row ever written. If pruning just deleted the oldest rows
outright, `verify_chain()` would recompute from GENESIS against a table
whose surviving first row was never actually chained from GENESIS -- a
false tamper alarm on every prune, not a real one.

Fix: every prune run records a checkpoint (`retention_checkpoints`) --
the content_hash of the last row it removes -- in the same transaction
as the delete, before committing. `events.verify_chain()` (and
`last_hash()`) start from the latest checkpoint's chain_tip_hash instead
of hardcoding GENESIS, so both verification and future writes still
chain correctly across a pruned history.

That checkpoint mechanism is also why prune only ever removes a
contiguous prefix of the chain (oldest rows first, by insertion/rowid
order), never an arbitrary filtered set: there has to be exactly one
clean boundary between "pruned" and "kept" for a single chain_tip_hash
to mean anything. In particular, if an event's occurred_at is out of
step with its rowid (clock skew across writers), pruning stops at the
first row -- in rowid order -- that isn't yet past the retention window,
even if a later row happens to look eligible; it never skips over a
fresher row to reach an older one further down. A row that stays behind
this run because of that just gets picked up on a later run once
everything ahead of it has also aged out.
"""
from __future__ import annotations

import calendar
import sqlite3
import time
from dataclasses import dataclass

from bf_agent_viewer.events import new_id

# ~6 months -- the compliance-safe default named in F-019's own
# description, sized against REQ-002 (EU AI Act Art. 19/26 minimum
# retention expectations), not just a round number.
DEFAULT_RETENTION_DAYS = 180.0


@dataclass(frozen=True)
class PruneResult:
    events_pruned: int
    cutoff: str
    chain_tip_hash: str | None  # None when this run had nothing to prune


def _parse_occurred_at(occurred_at: str) -> float:
    """occurred_at is always UTC/`Z` (see events/log.py) -- calendar.timegm
    interprets the struct_time as UTC directly, unlike time.mktime, which
    would apply the local timezone and silently shift the cutoff."""
    return calendar.timegm(time.strptime(occurred_at, "%Y-%m-%dT%H:%M:%SZ"))


def _cutoff_timestamp(retention_days: float, *, now: str | None = None) -> str:
    now_epoch = time.time() if now is None else _parse_occurred_at(now)
    cutoff_epoch = now_epoch - retention_days * 86400.0
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff_epoch))


def prune_events(
    conn: sqlite3.Connection,
    *,
    retention_days: float = DEFAULT_RETENTION_DAYS,
    now: str | None = None,
) -> PruneResult:
    """Deletes events older than `retention_days`, recording a
    retention_checkpoints row (the removed prefix's chain tip) in the
    same transaction so the tamper-evident hash chain stays verifiable
    across the deletion -- see the module docstring for why this can't
    be a plain DELETE.

    `now` overrides "the current time" for the cutoff calculation --
    real callers (the CLI) never pass it; tests do, so they don't need
    to seed events with today's timestamp to exercise this.

    Safe to call repeatedly (e.g. from cron, per the CLI docs): a run
    with nothing yet past the window is a no-op, and a database that's
    never had anything pruned behaves identically to one that has (see
    events/log.py's checkpoint fallback).
    """
    cutoff = _cutoff_timestamp(retention_days, now=now)

    rows = conn.execute(
        "SELECT rowid, id, occurred_at, content_hash FROM events ORDER BY rowid ASC"
    ).fetchall()

    prefix_end_rowid: int | None = None
    last_pruned: tuple[str, str, str] | None = None
    events_pruned = 0
    for rowid, event_id, occurred_at, content_hash in rows:
        if occurred_at >= cutoff:
            break
        prefix_end_rowid = rowid
        last_pruned = (event_id, occurred_at, content_hash)
        events_pruned += 1

    if prefix_end_rowid is None:
        return PruneResult(events_pruned=0, cutoff=cutoff, chain_tip_hash=None)

    pruned_event_id, pruned_occurred_at, chain_tip_hash = last_pruned

    conn.execute(
        """INSERT INTO retention_checkpoints
           (id, pruned_through_event_id, pruned_through_occurred_at,
            chain_tip_hash, events_pruned)
           VALUES (?, ?, ?, ?, ?)""",
        (new_id(), pruned_event_id, pruned_occurred_at, chain_tip_hash, events_pruned),
    )
    conn.execute("DELETE FROM events WHERE rowid <= ?", (prefix_end_rowid,))
    conn.commit()

    return PruneResult(events_pruned=events_pruned, cutoff=cutoff, chain_tip_hash=chain_tip_hash)
