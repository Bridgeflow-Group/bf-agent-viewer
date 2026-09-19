# How It Works

BF Agent Viewer -- living document. Explains how the system works as currently designed: for internal use to spot gaps before they're built, and for prospective customers evaluating whether to install it. Update this doc whenever a mechanism changes; don't let it drift from features.xlsx / open-questions.docx / versions.docx, which remain the detailed source of record.

Reflects design as of September 18, 2026. Nothing described here is built yet -- v0.1.0 is in design.


## 1. What this platform is

An open-source, self-hosted identity and visibility layer for AI agents. The first release answers one question well: what are my agents, and what are they actually doing? It does not enforce or block anything in v0.1.0 -- visibility comes first, control comes later, deliberately.


## 2. How an agent gets found and registered

There are two ways an agent enters the system, and both are supported:

- Explicit registration -- an agent, a CLI, or a CI/CD pipeline calls a registration endpoint ahead of time and receives an agent ID and platform-issued, short-lived credentials. An owner is assigned at creation. This is the only path that makes an agent kill-switch-ready from day one, since revocation only works on credentials the platform itself issued.
- Passive discovery -- an agent the platform has never seen shows up through normal traffic (the MCP gateway, or the SDK), and the platform auto-creates an unowned record for it, flagged on the dashboard until someone claims it. This exists because teams won't always remember to register every script before running it, and the platform still needs to see it either way.
The distinction that matters: a passively-discovered agent is visible but not yet controllable. "Claiming" it should mean both assigning an owner and rotating it onto platform-issued credentials -- otherwise it looks owned on a dashboard but still can't actually be shut off.


## 3. How activity actually gets captured

Two instrumentation paths, layered rather than a single universal SDK:

- MCP gateway (primary path) -- a proxy sits between agents and MCP servers and sees every tool call and resource access as it happens. This is the highest-fidelity path and doubles as a security fix: a large share of MCP servers today have no authentication of their own.
- SDK, OpenTelemetry-based (fallback) -- a lightweight SDK for agents not routed through MCP, following the OpenTelemetry GenAI semantic conventions rather than a proprietary format.
What's explicitly out of view in v0.1.0: an agent calling arbitrary APIs directly, with no MCP layer and no SDK integration, isn't visible to the platform. That's a stated boundary, not a bug -- widening this coverage is future work, not a v0.1.0 promise.


## 4. How agent identity works

An Agent Identity is a first-class, persistent object -- not a label attached to an API key. It carries an owner, an environment, its credential state, and the tools/resources it's touched. Every event the platform records ties back to an agent identity, and every agent identity ties back to an accountable human owner (or is flagged as missing one).


## 5. How sub-agents and delegation are handled

When an agent spawns a sub-agent, that's a delegation event, not an unrelated new identity. The sub-agent gets its own identity record, linked to its parent, and its granted permissions can only ever be a subset of its parent's -- authority narrows at every hop, it never widens. Every event a sub-agent produces still traces back through the chain to the original human or system that started the whole workflow, so accountability doesn't get lost just because work was delegated. By default, a sub-agent inherits its parent's owner and environment rather than showing up as ownerless.

Open gap: whether every sub-agent spawn -- including one that lives for a few seconds and never recurs -- creates a full, permanent identity record, or whether short-lived ones get something lighter-weight that's only promoted to a full identity if that role turns out to be recurring. Not yet decided (see open-questions.docx, OQ-007).


## 6. How the kill switch works (planned, v0.2.0)

Not built in v0.1.0. When it ships, it works by revoking short-lived, platform-issued credentials rather than intercepting every call in real time -- the platform simply stops renewing an agent's credentials, and its access lapses within the credential's TTL window. Target: under 5 minutes for a production agent, under 1 minute for one with transaction authority.

What it does not do: it can't undo an action already in flight (a database write already sent isn't rolled back), and it only covers activity that actually routes through the platform's enforcement point -- an agent's access outside that surface can't be revoked this way.


## 7. How it's deployed

Default self-hosted deployment is a single binary or container with an embedded SQLite (or libSQL) store -- no separate database process required to get started. Postgres is an explicit upgrade path once event volume or write concurrency actually needs it, not a day-one requirement. This is deliberately lighter than comparable self-hosted tools, which typically need three or four separate infrastructure components running at once.


## 8. Known open design questions (internal gap list)

Kept here in plain language for visibility; open-questions.docx has full detail and is the source of record. A prospective customer reading this should treat anything listed here as not yet final.

- OQ-001 -- confirming the SQLite-first deployment is genuinely low-friction enough for a team with no dedicated infra person, not just lighter on paper.
- OQ-002 -- validating the MCP-gateway-plus-SDK instrumentation approach against real agent setups.
- OQ-003 -- validating the delegation/identity-boundary model against real multi-agent architectures.
- OQ-004 -- kill switch mechanism is scoped (credential revocation) but not built or tested.
- OQ-006 -- dual-path registration is decided; the claiming/credential-rotation flow still needs to be specified in detail.
- OQ-007 -- whether every sub-agent spawn needs a full identity record, or something lighter for one-shot sub-agents.

## 9. Where to go for more detail

- versions.docx -- version numbers, status, and focus (source of record for versioning).
- features.xlsx -- every feature, its status, target version, and design notes.
- open-questions.docx -- full detail on every open/investigating/resolved design question.
- regulatory-requirements.docx -- how this maps to what governments and enterprise buyers require.
- AI_Agent_Identity_Platform_Design_Document.docx / AI_Agent_Identity_Platform_Research.docx -- original design and research documents this all builds on.