"""Alert channels (F-036): where a fired alert actually goes.

Every channel implements the same tiny interface -- send(alert) -> bool --
and every channel is fire-and-forget from the caller's perspective:
delivery failures are caught and logged here, never raised, because an
alert channel going down (a webhook endpoint timing out, an SMTP server
being unreachable) must never take the gateway's main request path down
with it. See service.py's fire_alert() for where that boundary actually
matters (a rate-limit rejection has to raise regardless of whether the
alert about it could be delivered).

Only stdlib: urllib.request for the webhook, smtplib/email for email --
no new dependency for "basic" alerting (F-036's own scoping), consistent
with the project's low-infra-footprint positioning (F-025).
"""
from __future__ import annotations

import json
import logging
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Protocol

logger = logging.getLogger("bf_agent_viewer.alerts")


class Alert(Protocol):
    id: str
    organization_id: str
    agent_id: str | None
    alert_type: str
    severity: str
    message: str
    metadata: dict


class AlertChannel(Protocol):
    def send(self, alert: Alert) -> bool:
        """Returns True on successful delivery, False on failure. Must
        never raise -- a channel that can fail (network, SMTP) catches its
        own errors and returns False; fire_alert() logs the persisted
        alert's delivered status either way."""
        ...


class LoggingAlertChannel:
    """The always-available fallback: logs the alert at WARNING (or ERROR
    for severity="critical") via the standard logging module. This is
    what a gateway with no webhook/email configured actually uses -- not
    a no-op. An unconfigured alert channel silently dropping alerts is
    exactly the kind of gap this project's own standing practice argues
    against; this is the real, always-on floor every deployment gets,
    configured or not."""

    def send(self, alert: Alert) -> bool:
        level = logging.ERROR if alert.severity == "critical" else logging.WARNING
        logger.log(
            level, "ALERT [%s/%s] org=%s agent=%s: %s",
            alert.severity, alert.alert_type, alert.organization_id, alert.agent_id, alert.message,
        )
        return True


class WebhookAlertChannel:
    """POSTs a JSON payload to `url`. Generic by design -- works as-is
    with anything that accepts a JSON POST (a generic incoming webhook,
    a small internal receiver); Slack/PagerDuty/etc.-specific payload
    shaping is a real but separate feature (their APIs expect their own
    JSON shapes), not part of F-036's "basic" scope."""

    def __init__(self, url: str, *, timeout: float = 5.0, headers: dict[str, str] | None = None):
        self.url = url
        self.timeout = timeout
        self.headers = headers or {}

    def send(self, alert: Alert) -> bool:
        payload = json.dumps({
            "id": alert.id,
            "organization_id": alert.organization_id,
            "agent_id": alert.agent_id,
            "alert_type": alert.alert_type,
            "severity": alert.severity,
            "message": alert.message,
            "metadata": alert.metadata,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=payload, method="POST",
            headers={"Content-Type": "application/json", **self.headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ok = 200 <= resp.status < 300
                if not ok:
                    logger.warning("webhook alert channel: %s returned status %s", self.url, resp.status)
                return ok
        except (urllib.error.URLError, OSError) as e:
            logger.warning("webhook alert channel: failed to reach %s: %s", self.url, e)
            return False


class EmailAlertChannel:
    """Sends a plain-text email via SMTP. STARTTLS by default (use_tls);
    username/password are optional (an internal relay may accept
    unauthenticated mail from an allowlisted host)."""

    def __init__(
        self, *, smtp_host: str, smtp_port: int = 587, from_addr: str, to_addrs: list[str],
        username: str | None = None, password: str | None = None, use_tls: bool = True,
        timeout: float = 10.0,
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.from_addr = from_addr
        self.to_addrs = to_addrs
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.timeout = timeout

    def send(self, alert: Alert) -> bool:
        msg = EmailMessage()
        msg["Subject"] = f"[BF Agent Viewer] {alert.severity.upper()}: {alert.alert_type}"
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.to_addrs)
        body = [
            alert.message, "",
            f"organization: {alert.organization_id}",
            f"agent: {alert.agent_id or '(none)'}",
            f"alert id: {alert.id}",
        ]
        if alert.metadata:
            body.append(f"metadata: {json.dumps(alert.metadata)}")
        msg.set_content("\n".join(body))

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout) as smtp:
                if self.use_tls:
                    smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password or "")
                smtp.send_message(msg)
            return True
        except (smtplib.SMTPException, OSError) as e:
            logger.warning("email alert channel: failed to send via %s: %s", self.smtp_host, e)
            return False


class CompositeAlertChannel:
    """Fans an alert out to every channel in `channels`, independently --
    one channel failing doesn't stop the others from being tried. Returns
    True if at least one channel delivered successfully."""

    def __init__(self, channels: list[AlertChannel]):
        self.channels = channels

    def send(self, alert: Alert) -> bool:
        if not self.channels:
            return False
        results = []
        for channel in self.channels:
            try:
                results.append(channel.send(alert))
            except Exception as e:  # noqa: BLE001 -- a channel must never take the caller down
                logger.warning("alert channel %r raised instead of returning False: %s", channel, e)
                results.append(False)
        return any(results)
