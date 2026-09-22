CREATE TABLE organizations (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    settings        TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE humans (
    id                  TEXT PRIMARY KEY,
    organization_id     TEXT NOT NULL REFERENCES organizations(id),
    name                TEXT NOT NULL,
    role                TEXT,
    email               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE agents (
    id                  TEXT PRIMARY KEY,
    organization_id     TEXT NOT NULL REFERENCES organizations(id),
    name                TEXT NOT NULL,
    owner_id            TEXT NOT NULL REFERENCES humans(id),
    technical_owner_id  TEXT REFERENCES humans(id),
    environment         TEXT,
    status              TEXT NOT NULL DEFAULT 'unclaimed',
    autonomy_tier       TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_agents_owner ON agents(owner_id);
CREATE INDEX idx_agents_status ON agents(status);

CREATE TABLE agent_identities (
    id                  TEXT PRIMARY KEY,
    agent_id            TEXT NOT NULL REFERENCES agents(id),
    identity_type       TEXT NOT NULL,
    subject             TEXT NOT NULL,
    issuer              TEXT NOT NULL,
    valid_from          TEXT NOT NULL,
    valid_to            TEXT,
    parent_identity_id  TEXT REFERENCES agent_identities(id),
    granted_scope       TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_identities_agent ON agent_identities(agent_id);
CREATE INDEX idx_identities_parent ON agent_identities(parent_identity_id);

CREATE TABLE credentials (
    id              TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL REFERENCES agents(id),
    type            TEXT NOT NULL,
    issuer          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    issued_at       TEXT NOT NULL DEFAULT (datetime('now')),
    expiry          TEXT,
    revoked_at      TEXT,
    granted_scope   TEXT
);
CREATE INDEX idx_credentials_agent ON credentials(agent_id);
CREATE INDEX idx_credentials_status ON credentials(status);

CREATE TABLE tools (
    id          TEXT PRIMARY KEY,
    provider    TEXT,
    name        TEXT NOT NULL,
    endpoint    TEXT
);

CREATE TABLE resources (
    id          TEXT PRIMARY KEY,
    type        TEXT,
    provider    TEXT,
    locator     TEXT
);

CREATE TABLE sessions (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL REFERENCES agents(id),
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at    TEXT
);
CREATE INDEX idx_sessions_agent ON sessions(agent_id);

CREATE TABLE events (
    id                  TEXT PRIMARY KEY,
    event_version       TEXT NOT NULL DEFAULT 'v1',
    occurred_at         TEXT NOT NULL,
    organization_id     TEXT NOT NULL REFERENCES organizations(id),
    agent_id            TEXT NOT NULL REFERENCES agents(id),
    session_id          TEXT REFERENCES sessions(id),
    actor_human_id      TEXT REFERENCES humans(id),
    event_type          TEXT NOT NULL,
    action              TEXT,
    tool_id             TEXT REFERENCES tools(id),
    resource_id         TEXT REFERENCES resources(id),
    environment         TEXT,
    source              TEXT,
    result              TEXT,
    request_id          TEXT,
    metadata            TEXT,
    content_hash        TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_events_agent_time ON events(agent_id, occurred_at);
CREATE INDEX idx_events_session ON events(session_id);
CREATE INDEX idx_events_type_time ON events(event_type, occurred_at);
CREATE INDEX idx_events_actor ON events(actor_human_id);
