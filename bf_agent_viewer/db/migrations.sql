-- Additive schema, run on every connect() (not just on first creation),
-- always as CREATE-IF-NOT-EXISTS. schema.sql only ever runs once, against
-- a brand-new database file (see connection.py's docstring/history --
-- OQ-003 already burned us once on destructive-on-every-start behavior),
-- so a table added after v0.1.0's initial release goes here instead,
-- where existing installs pick it up on their next process start without
-- a separate migration step to run by hand.
--
-- console_credentials / console_sessions / console_pending_logins back
-- F-040 (console authentication): password + mandatory TOTP MFA, hardened
-- sessions. See bf_agent_viewer/console/auth.py for the mechanism.

CREATE TABLE IF NOT EXISTS console_credentials (
    human_id            TEXT PRIMARY KEY REFERENCES humans(id),
    password_hash       TEXT NOT NULL,
    password_salt       TEXT NOT NULL,
    totp_secret         TEXT NOT NULL,
    totp_enabled        INTEGER NOT NULL DEFAULT 0,
    failed_attempts      INTEGER NOT NULL DEFAULT 0,
    locked_until        TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Real, authenticated sessions. Only a SHA-256 hash of the session token
-- is ever stored -- reading this table doesn't hand you a working
-- session, matching how `credentials`/bearer tokens work elsewhere in
-- this project (see identity/registry.py issue_token), just applied to
-- the console's own login flow rather than agent identity.
CREATE TABLE IF NOT EXISTS console_sessions (
    token_hash          TEXT PRIMARY KEY,
    human_id            TEXT NOT NULL REFERENCES humans(id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at          TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_console_sessions_human ON console_sessions(human_id);

-- Short-lived, unauthenticated-yet record of "password just checked out,
-- waiting on the TOTP code" -- deliberately separate from console_sessions
-- so a pending row can never be mistaken for (or reused as) a real,
-- fully-authenticated session, and so it can carry its own much shorter TTL.
CREATE TABLE IF NOT EXISTS console_pending_logins (
    token_hash          TEXT PRIMARY KEY,
    human_id            TEXT NOT NULL REFERENCES humans(id),
    purpose             TEXT NOT NULL DEFAULT 'login',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at          TEXT NOT NULL
);

-- alerts (F-036): persisted independently of delivery -- `delivered`
-- records what actually happened, not what was attempted, so a fired
-- alert whose webhook/email failed is still visible later (a future
-- console alerts view, not yet built, would read this table), not lost.
-- See bf_agent_viewer/alerts/service.py.
CREATE TABLE IF NOT EXISTS alerts (
    id                  TEXT PRIMARY KEY,
    organization_id     TEXT NOT NULL REFERENCES organizations(id),
    agent_id            TEXT REFERENCES agents(id),
    alert_type          TEXT NOT NULL,
    severity            TEXT NOT NULL,
    message             TEXT NOT NULL,
    metadata            TEXT,
    delivered           INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_alerts_org_time ON alerts(organization_id, created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_agent ON alerts(agent_id);
