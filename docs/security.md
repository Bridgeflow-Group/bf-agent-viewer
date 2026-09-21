# Security & Architecture

How BF Agent Viewer is built, and — just as important — what it does and doesn't do yet. This is a design document, not an audit; nothing here is built yet (see [`versions.md`](versions.md)).

## Principles

- The platform itself is treated as security-sensitive infrastructure, not a passive logging shelf.
- No third-party standing secrets are stored in plaintext.
- Ingestion authentication (how the platform authenticates incoming events) is kept separate from agent identity (who the event is actually about) — conflating the two is how shared-credential messes happen.
- Least privilege applies to the platform's own database and integrations, the same standard it asks agents to meet.

## What v0.1.0 actually does

v0.1.0 is visibility only. It registers agents, captures their activity, and tracks who owns them. It does not intercept, block, or evaluate any action — see [`positioning.md`](positioning.md) for why that sequencing is deliberate, not a missing feature.

**Identity.** Every agent gets a first-class identity record, not just an API-key label. When one agent delegates to another (an orchestrator spawning a sub-agent, for example), the sub-agent gets its own identity, linked to its parent, and its permissions are always a subset of its parent's — authority only ever narrows moving down a delegation chain, never widens. Every event traces back through that chain to an accountable human owner.

**Credentials.** Agents entering the system explicitly (via API, CLI, or CI/CD) get platform-issued credentials from day one, scoped to an explicit allowlist of write actions rather than a blanket grant. Agents discovered passively (seen in traffic before anyone registered them) show up as visible-but-unowned until claimed — claiming means assigning an owner *and* rotating the agent onto platform-issued credentials, not just naming it.

**Tamper-evident event records.** Each event's hash is chained to the previous event's hash, so altering or deleting a past event breaks the chain from that point forward — detectable by re-verifying it. The chain's checkpoint (the "last known good" reference point) is written to a separate file from the main database, using append-only file semantics where the OS supports it, so a compromise of the database file alone can't silently regenerate a consistent-looking history.

**Deployment.** Self-hosted, single-tenant per install — one organization per instance, not a shared multi-tenant service. Default storage is embedded SQLite/libSQL; no separate database server required to start.

## What v0.1.0 explicitly does not do

- No policy enforcement or action-blocking. That's the difference between visibility and control, and control comes later.
- No kill switch. Stopping a misbehaving agent is a deliberate v0.2.0 capability (credential revocation, not real-time interception), not part of the first release.
- No multi-tenant isolation enforcement. Single-tenant self-hosted doesn't need it; a future hosted offering would.
- Coverage gap by design: an agent calling APIs directly, with no MCP layer and no SDK integration, isn't visible to the platform yet. See [`how-it-works.md`](how-it-works.md) for the instrumentation approach and its known boundary.

## Why this order

A kill switch without a clear record of what happened tells you *that* something went wrong, not *which* action crossed the line. Building control before visibility is how you end up with a system you can't actually operate — so this is built visibility-first, deliberately, even though it means v0.1.0 can't stop anything yet.
