"""Alert persistence + firing (F-036).

Every alert is written to the `alerts` table first, then handed to a
channel for delivery -- persistence never depends on delivery succeeding.
That matters for the same reason tamper-evident event logging exists at
all: a webhook endpoint being down shouldn't mean an alert never
happened, just that it wasn't delivered live. `delivered` records what
actually happened, not what was attempted, so a future console alerts
view (not yet built) can show "fired but undelivered" rather than
nothing.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field

from bf_agent_viewer.events import new_id

logger = logging.getLogger("bf_agent_viewer.alerts")

VALID_SEVERITIES = ("info", "warning", "critical")


@dataclass(frozen=True)
class Alert:
    id: str
    organization_id: str
    agent_id: str | None
    alert_type: str
    severity: str
    message: str
    metadata: dict = field(default_factory=dict)
    created_at: str = ""


def fire_alert(
    conn: sqlite3.Connection,
    channel,
    *,
    organization_id: str,
    alert_type: str,
    message: str,
    agent_id: str | None = None,
    severity: str = "warning",
    metadata: dict | None = None,
) -> Alert:
    """Persists the alert, then attempts delivery through `channel`.
    Never raises on a delivery failure -- channel.send() itself is
    contracted not to raise (see channel.py), and this function catches
    the pathological case of a misbehaving channel anyway, since the
    caller (e.g. the gateway's rate-limit check, mid-request) must not
    be taken down by an alerting failure."""
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"severity must be one of {VALID_SEVERITIES}, got {severity!r}")

    alert_id = new_id()
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    metadata = metadata or {}

    conn.execute(
        """INSERT INTO alerts
               (id, organization_id, agent_id, alert_type, severity, message, metadata,
                delivered, created_at)
           VALUES (?,?,?,?,?,?,?,0,?)""",
        (alert_id, organization_id, agent_id, alert_type, severity, message, json.dumps(metadata), created_at),
    )
    conn.commit()

    alert = Alert(
        id=alert_id, organization_id=organization_id, agent_id=agent_id, alert_type=alert_type,
        severity=severity, message=message, metadata=metadata, created_at=created_at,
    )

    try:
        delivered = bool(channel.send(alert))
    except Exception as e:  # noqa: BLE001 -- alerting must never break the caller
        logger.error("alert channel %r raised sending alert %s: %s", channel, alert_id, e)
        delivered = False

    conn.execute("UPDATE alerts SET delivered = ? WHERE id = ?", (1 if delivered else 0, alert_id))
    conn.commit()

    return alert
