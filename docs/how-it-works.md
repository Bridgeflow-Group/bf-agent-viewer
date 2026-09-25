# How It Works

BF Agent Viewer -- living document. Explains how the system works as currently designed: for internal use to spot gaps before they're built, and for prospective customers evaluating whether to install it. Update this doc whenever a mechanism changes; don't let it drift from [`features.md`](features.md) / [`versions.md`](versions.md), which remain the detailed source of record.

Design as of September 18, 2026, updated as mechanisms actually get built. Real implementation of v0.1.0 started September 22, 2026 -- most of what's described below is now built and tested; see [`status.md`](status.md) for exactly what's built vs. still planned.


## 1. What this platform is

An open-source, self-hosted identity and visibility layer for AI agents. The first release answers one question well: what are my agents, and what are they actually doing? Visibility came first, deliberately -- and once it existed, one narrow piece of control came with it in the same release: `credential revoke`, an immediate kill switch (section 6, below). v0.1.0 does not do fine-grained, real-time policy enforcement over individual calls -- that's still later work -- but "no enforcement at all" is no longer accurate as of this release.


## 2. How an agent gets found and registered

There are two ways an agent enters the system, and both are supported:

- Explicit registration -- an agent, a CLI, or a CI/CD pipeline calls a registration endpoint ahead of time and receives an agent ID and platform-issued, short-lived credentials. An owner is assigned at creation. This is the only path that makes an agent kill-switch-ready from day one, since revocation only works on credentials the platform itself issued.
- Passive discovery -- an agent the platform has never seen shows up through normal traffic (the MCP gateway, or the SDK), and the platform auto-creates an unowned record for it, flagged on the dashboard until someone claims it. This exists because teams won't always remember to register every script before running it, and the platform still needs to see it either way.
The distinction that matters: a passively-discovered agent is visible but not yet controllable. "Claiming" it should mean both assigning an owner and rotating it onto platform-issued credentials -- otherwise it looks owned on a dashboard but still can't actually be shut off.


## 3. How activity actually gets captured

Two instrumentation paths, layered rather than a single universal SDK:

- MCP gateway (primary path, F-023, built) -- a proxy sits between agents and MCP servers and sees every tool call and resource access as it happens. This is the highest-fidelity path and doubles as a security fix: a large share of MCP servers today have no authentication of their own.
- SDK, OpenTelemetry-based (fallback, F-024, built) -- a lightweight SDK for agents not routed through MCP, following the OpenTelemetry GenAI semantic conventions (`gen_ai.*` attribute names) rather than a proprietary format. `bf_agent_viewer.sdk.BFAgentViewerClient`, a dependency-free (stdlib-only) Python client, POSTs a completed tool call's details to a small HTTP route on the same gateway process (`POST /v1/otel/events`), authenticated with the same bearer token the MCP path uses -- one identity system, two ways in. The real difference from the MCP path isn't the wire format, it's *when* the platform learns about the call: the gateway sees it as it happens because it sits in the request path; the SDK only sees what an agent chooses to report, after the fact, which is exactly why this is the lower-fidelity fallback and not a second primary path. That also means this path can log a scope violation or an ingestion-rate-limit breach (and, for the former, raise an alert) but can never actually block anything -- there's no proxy step here to deny, the real call already happened out-of-band. See [`CLI.md`](CLI.md) for usage and [`security.md`](security.md) for what this endpoint does and doesn't protect against.
What's explicitly out of view in v0.1.0: an agent calling arbitrary APIs directly, with no MCP layer and no SDK integration, isn't visible to the platform. That's a stated boundary, not a bug -- widening this coverage is future work, not a v0.1.0 promise.


## 4. How agent identity works

An Agent Identity is a first-class, persistent object -- not a label attached to an API key. It carries an owner, an environment, its credential state, and the tools/resources it's touched. Every event the platform records ties back to an agent identity, and every agent identity ties back to an accountable human owner (or is flagged as missing one).


## 5. How sub-agents and delegation are handled

When an agent spawns a sub-agent, that's a delegation event, not an unrelated new identity. The sub-agent gets its own identity record, linked to its parent, and its granted permissions can only ever be a subset of its parent's -- authority narrows at every hop, it never widens. Every event a sub-agent produces still traces back through the chain to the original human or system that started the whole workflow, so accountability doesn't get lost just because work was delegated. By default, a sub-agent inherits its parent's owner and environment rather than showing up as ownerless.

Resolved: every sub-agent spawn -- including one that lives for a few seconds and never recurs -- gets its own full, permanent identity record; there's no lighter-weight tier. What's still undecided is a narrower follow-on question: whether a recurring sub-agent *role* should eventually get "promoted" to its own first-class top-level dashboard row instead of always nesting under its parent (see [`status.md`](status.md)).


## 6. How the kill switch works (built, v0.1.0)

Originally scoped for v0.2.0, but pulled forward once building T-022 (the kill switch itself) turned out to need T-024 (short-lived, platform-issued credentials) as a real prerequisite anyway -- see [`status.md`](status.md). Works by revocation, not by intercepting every call in real time: `bf-agent-viewer credential revoke` immediately marks a credential unusable, and any credential can optionally be issued with a TTL (`register --ttl-seconds`/`claim`, extended before it lapses with `credential renew`) so it expires on its own without an explicit revoke call. A running gateway process doesn't need restarting to pick either one up -- it reloads its in-memory credential list from the database on a short, configurable interval (`--credential-refresh-seconds`, default 30s), well under the sub-5-minute target.

A real security property, not just a documented promise: a credential that's been revoked or has expired and is still *presented* is rejected outright, not treated the same as a caller with no credential at all -- the two cases used to be folded together, which meant revoking a credential actually left an agent *less* restricted (unscoped, via passive discovery) than before. Fixed as part of building this; see [`security.md`](security.md).

What it does not do: it can't undo an action already in flight (a database write already sent isn't rolled back), it only covers activity that actually routes through the platform's own enforcement point (the gateway or the OTel ingestion route), and it's revocation of an agent's *whole* credential, not fine-grained blocking of one specific tool call while leaving the rest of its access intact -- that level of real-time, per-call policy enforcement is still later work (v0.3.0's runtime policy engine, [`versions.md`](versions.md)).


## 7. How it's deployed

Default self-hosted deployment is a single binary or container with an embedded SQLite (or libSQL) store -- no separate database process required to get started. Postgres is an explicit upgrade path once event volume or write concurrency actually needs it, not a day-one requirement. This is deliberately lighter than comparable self-hosted tools, which typically need three or four separate infrastructure components running at once.


## 8. Known open design questions

Kept here in plain language for visibility. Most of the early open questions below are now resolved as the mechanisms they were about actually got built (see the sections above); kept here for context rather than deleted outright, so a returning reader can see what used to be uncertain.

- OQ-001 -- resolved. The SQLite-first deployment is confirmed genuinely lower-friction than the closest comparable self-hosted tool (which needs four separate infrastructure components; this needs one).
- OQ-002 -- resolved. The MCP-gateway-plus-SDK instrumentation approach (section 3, above) has been validated against multiple independent real agent frameworks, not just designed on paper.
- OQ-003 -- resolved. The identity boundary is created at every delegation hop, not one fixed line -- see section 5, above.
- OQ-004 -- resolved, and built. The kill switch (section 6, above) shipped in v0.1.0 as credential revocation, well under the sub-5-minute target (default 30s propagation on a live gateway).
- OQ-006 -- resolved. Dual-path registration (explicit + passive discovery, section 2, above) is built and tested.
- OQ-007 -- resolved. See section 5, above.

## 9. Where to go for more detail

- [`versions.md`](versions.md) -- version numbers, status, and focus (source of record for versioning).
- [`features.md`](features.md) -- every feature, its status, target version, and what it does.
- [`status.md`](status.md) -- exactly what's built, tested, and verified right now, vs. still planned or blocked.
- [`security.md`](security.md) -- the platform's security architecture and what it does and doesn't cover yet.
- [`standards.md`](standards.md) -- the technical/security standards this design follows, tracks, or has deliberately not adopted, and why.
- [`regulatory.md`](regulatory.md) -- how this maps to what governments and enterprise buyers require.
- [`research.md`](research.md) (continued in [`research-part-2.md`](research-part-2.md)) -- the running research and validation log this design is built on.