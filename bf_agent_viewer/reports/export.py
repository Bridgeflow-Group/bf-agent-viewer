"""Customer-facing compliance export (F-034/T-016).

Per OQ-011's resolved decision: a filtered CSV export of event history --
by agent, date range, and owner -- so a customer can satisfy their own
regulatory record-keeping obligations (e.g. EU AI Act Art. 12, state
ADMT-type laws) without contacting us. Deliberately distinct from F-020
(SIEM export): different audience (the customer's own compliance/audit
function, not a security team watching a live feed) and a different
shape (a static report generated on request, not a streaming
integration). Formatted/PDF output was considered and deferred to a
possible later Paid enhancement -- this is CSV only, built entirely on
the existing event store with no new pipeline, matching the low-infra
self-hosted positioning (F-025).
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

CSV_HEADER = [
    "event_id", "occurred_at", "organization_id", "agent_id", "agent_name",
    "owner_human_id", "owner_name", "event_type", "action", "tool_id",
    "resource_id", "environment", "source", "result", "session_id",
    "actor_human_id", "request_id", "metadata", "content_hash",
]


class InvalidDateBoundError(ValueError):
    """Raised when `start`/`end` isn't a recognized date/timestamp shape.
    A ValueError subclass so existing `except ValueError` call sites (the
    console route, below) keep working without special-casing this."""


def _normalize_bound(value: str | None, *, is_end: bool) -> str | None:
    """Accepts either a bare date (`2026-09-01`) or a full occurred_at-shape
    timestamp (`2026-09-01T00:00:00Z`). occurred_at strings sort correctly
    as plain text (ISO 8601, fixed width, UTC/`Z` throughout -- the same
    property `events/log.py`'s own chain-ordering already relies on), so a
    bare start date works as an inclusive lower bound with no change: any
    same-day timestamp is lexically greater than its own date prefix. An
    end date needs the opposite treatment -- as a bare prefix it would
    exclude every timestamp on that day, which isn't what "through
    2026-09-01" should mean for a compliance export -- so it's expanded to
    the last representable second of that day. Anything that isn't one of
    these two exact shapes is rejected outright rather than silently
    passed through to SQLite, where a malformed bound would just produce a
    confusing empty (or wrong) result set instead of a clear error."""
    if value is None or value == "":
        return None
    if _TIMESTAMP_RE.match(value):
        return value
    if _DATE_RE.match(value):
        return f"{value}T23:59:59Z" if is_end else value
    raise InvalidDateBoundError(
        f"{value!r} is not a recognized date -- use YYYY-MM-DD or the full "
        f"YYYY-MM-DDTHH:MM:SSZ timestamp shape"
    )


def export_events_csv(
    conn: sqlite3.Connection,
    *,
    organization_id: str | None = None,
    agent_id: str | None = None,
    owner_human_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> str:
    """Returns the filtered event history as CSV text (including header
    row), ordered chronologically (oldest first) -- an audit trail reads
    top-to-bottom as "what happened, in order," not most-recent-first like
    the dashboard's own live activity feed. `content_hash` is included so
    an auditor can independently verify the tamper-evident chain (see
    `events.verify_chain`) rather than taking the export on faith.

    Every filter is optional and additive (AND'd together): omitting all
    of them exports the organization's (or, with organization_id also
    omitted, the whole database's) complete event history. `organization_id`
    is a real filter here for the same reason it is everywhere else in
    this codebase post-T-014 -- a console/CLI invocation scoped to one
    organization must not be able to export another organization's
    events by supplying a different agent_id/owner_human_id.
    """
    start = _normalize_bound(start, is_end=False)
    end = _normalize_bound(end, is_end=True)

    conditions: list[str] = []
    params: list[str] = []
    if organization_id is not None:
        conditions.append("e.organization_id = ?")
        params.append(organization_id)
    if agent_id is not None:
        conditions.append("e.agent_id = ?")
        params.append(agent_id)
    if owner_human_id is not None:
        conditions.append("a.owner_id = ?")
        params.append(owner_human_id)
    if start is not None:
        conditions.append("e.occurred_at >= ?")
        params.append(start)
    if end is not None:
        conditions.append("e.occurred_at <= ?")
        params.append(end)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    rows = conn.execute(
        f"""
        SELECT e.id, e.occurred_at, e.organization_id, e.agent_id, a.name,
               a.owner_id, h.name, e.event_type, e.action, e.tool_id,
               e.resource_id, e.environment, e.source, e.result, e.session_id,
               e.actor_human_id, e.request_id, e.metadata, e.content_hash
        FROM events e
        LEFT JOIN agents a ON a.id = e.agent_id
        LEFT JOIN humans h ON h.id = a.owner_id
        {where}
        ORDER BY e.occurred_at ASC, e.rowid ASC
        """,
        tuple(params),
    ).fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)
    writer.writerows(rows)
    return buf.getvalue()
