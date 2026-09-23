"""Gateway-outage gap marker + alert (F-042).

v0.1.0 fails open by design (see security.md): if the gateway is down or
unreachable, agent traffic passes through unlogged rather than being
blocked. This module is the counterpart to that decision (OQ-016) -- the
gap in the record is never silent. On every gateway startup, it checks
how long it's been since the last event was logged. A short gap is just
a normal restart; a gap past the configured threshold means agent
traffic may have gone unrecorded in between, and that gets written down
as an explicit event and fired as an alert (F-036), rather than the
record just quietly picking back up as if nothing happened.

What this can't do: detect an outage *while it's happening* -- there's
no process running to notice. It can only look backward at startup and
say "there's a gap here, and here's how big it was." That's a real
limitation, stated plainly rather than implied away: a gateway that
never restarts after going down would never get its gap marked. In
practice a supervisor (systemd, Docker's own restart policy, k8s) is
what makes "goes down -> comes back up" actually true, and this is what
runs the moment it does.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from bf_agent_viewer.alerts import AlertChannel, LoggingAlertChannel, fire_alert
from bf_agent_viewer.events import last_hash, log_event

logger = logging.getLogger("bf_agent_viewer.gateway.resilience")

# Not a real agent -- mirrors the same sentinel-id pattern middleware.py
# already uses for FALLBACK_AGENT_ID. Safe because this project's own
# db/connection.py deliberately leaves foreign_keys off for now (see its
# docstring); if that ever changes, this needs a real system-agent row.
GAP_MARKER_AGENT_ID = "agent-gateway"

DEFAULT_GAP_THRESHOLD_SECONDS = 60.0
# Past this, the gap is severe enough to escalate past a routine restart
# blip -- an hour of unrecorded traffic is a materially different risk
# than a 90-second process restart, so it gets severity="critical"
# instead of "warning".
CRITICAL_GAP_THRESHOLD_SECONDS = 3600.0


def _parse_occurred_at(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def check_and_log_gap(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    alert_channel: AlertChannel | None = None,
    gap_threshold_seconds: float = DEFAULT_GAP_THRESHOLD_SECONDS,
    now: datetime | None = None,
) -> str | None:
    """Call once per gateway startup, before any request-serving
    middleware picks up the chain-tip hash (last_hash(conn)) for itself --
    this must run first so a logged gap event becomes part of the chain
    those later reads see, not a fork off to the side.

    Returns the new event id if a gap was logged, None if there was
    nothing to report (no prior events at all -- a first-ever startup,
    not a gap; or the elapsed time was under the threshold).
    """
    row = conn.execute(
        "SELECT id, occurred_at FROM events ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        # Nothing has ever been logged -- this is the very first startup,
        # not a gap coming back from anywhere.
        return None

    last_event_id, last_occurred_at = row
    last_time = _parse_occurred_at(last_occurred_at)
    now = now or datetime.now(timezone.utc)
    elapsed = (now - last_time).total_seconds()

    if elapsed < gap_threshold_seconds:
        return None

    gap_start = last_occurred_at
    gap_end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    severity = "critical" if elapsed >= CRITICAL_GAP_THRESHOLD_SECONDS else "warning"

    prev_hash = last_hash(conn)
    event_id, _new_hash = log_event(
        conn, organization_id=organization_id, agent_id=GAP_MARKER_AGENT_ID,
        session_id=None, actor_human_id=None, event_type="gateway.gap",
        action=None, result="gap_detected",
        metadata={
            "gap_start": gap_start, "gap_end": gap_end,
            "duration_seconds": round(elapsed, 1),
            "last_event_before_gap": last_event_id,
            "threshold_seconds": gap_threshold_seconds,
        },
        prev_hash=prev_hash,
    )
    conn.commit()

    logger.warning(
        "gateway gap detected: %s to %s (%.1fs) -- traffic in this window was not logged",
        gap_start, gap_end, elapsed,
    )

    channel = alert_channel or LoggingAlertChannel()
    fire_alert(
        conn, channel, organization_id=organization_id, agent_id=None,
        alert_type="gateway_gap", severity=severity,
        message=f"gateway was down/unreachable from {gap_start} to {gap_end} ({round(elapsed)}s) -- agent traffic in this window was not logged",
        metadata={"gap_start": gap_start, "gap_end": gap_end, "duration_seconds": round(elapsed, 1)},
    )

    return event_id
