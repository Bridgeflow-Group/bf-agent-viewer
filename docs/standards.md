# Standards & Industry Best Practices

Which technical standards and security best-practice frameworks this design follows, tracks, or has deliberately not adopted yet, and why. This is a different layer from [`regulatory.md`](regulatory.md) (law and compliance frameworks like the EU AI Act) — this doc is about the technical and security-community standards for how agent identity, credentials, and gateways should actually be built. Updated as the research log ([`research.md`](research.md)) turns up something that changes a decision here.

Status values: **adopted** (built into the design), **aligned** (the design already satisfies this without having targeted it directly), **planned** (decided, not yet built), **watching** (tracked, not yet acted on), **not adopted** (considered and declined, with reasons).

## MCP security (Model Context Protocol's own official guidance)

Directly relevant, not just background reading — this platform's gateway is the kind of MCP proxy this guidance is written for.

- **Token passthrough is forbidden.** An MCP proxy must never accept or forward a token that wasn't issued specifically for it (audience validation). **Planned** — a hard requirement for the v0.2.0 credential broker (F-037/038), not yet built.
- **Confused deputy protection.** Any proxy using a static client ID toward a third-party authorization server, with clients able to dynamically register, must implement per-client consent before forwarding to that third party. **Planned** — applies the moment the broker starts brokering third-party OAuth tokens on an agent's behalf.
- **stdio proxy sandboxing.** A proxy that spawns MCP servers as child processes via stdio (this platform's gateway pattern) is a privilege-escalation path if the proxy's own auth is ever compromised — spawned processes should be sandboxed (restricted filesystem/network access) and all stdio usage logged. **Adopted** — see [`security.md`](security.md) for the mechanism and [`status.md`](status.md) for build status. F-044/ISS-017 closed.
- **Scope minimization / progressive elevation.** Start with a minimal scope, elevate via explicit challenge rather than granting everything up front. **Aligned, partially** — the validated granted_scope model (a hard allowlist checked once per connection) satisfies the minimization goal; progressive step-up elevation is a considered v0.2.0+ refinement, not adopted yet.
- **State handle hijacking.** If a gateway ever mints its own session/workflow handles, they must be bound server-side to the authenticated identity, never trusted on possession alone. **Watching** — not yet relevant; the current design doesn't mint its own handles.
- **Audit-log field completeness, including argument redaction.** Gateway audit logs should capture requesting identity, tool name, input parameters (sensitive values redacted), timestamp, response status, and duration. **Partially aligned** — identity, tool, timestamp, result, and duration are all captured; input parameters are captured in full but with no redaction of sensitive values at all (see `research-part-2.md`, 2026-09-24). Real, undecided gap — tracked as OQ-027, not yet resolved, since the EU AI Act's own minimum-logging requirement (see `regulatory.md`) pulls toward keeping full input data, not redacting it.
- **MCP gateway table-stakes (external validation, 2026-09-24).** A March 2026 market survey of the now-real MCP gateway category confirms the baseline this design already targets — structured audit logging, IdP-integrated authN + RBAC/ABAC, rate limiting/allowlists, and workload isolation. **Aligned** — see `research-part-2.md` for the full comparison; no decision changed, this just confirms the bar was set correctly before a market existed to check it against.

## OWASP Top 10 for Agentic Applications (2026)

Most of the list (goal hijack, tool misuse, unexpected code execution, memory/context poisoning) governs an agent's own reasoning and tool execution — out of scope by design, since this platform instruments agents rather than building or running them. Three items are directly in scope:

- **ASI03 — Identity & Privilege Abuse.** This is the platform's core thesis. **Aligned** — unique bounded identity, short-lived scoped credentials, delegation-chain tracking, and enforced scope narrowing are built and validated against real traffic (see `research.md`, OQ-003).
- **ASI08 — Cascading Failures.** Calls for task-scoped credentials, rate limiting, and tamper-evident audit logs with lineage. **Aligned** — F-041 (per-agent rate limiting) and the hash-chained event log cover this.
- **ASI10 — Rogue Agents.** Calls for behavioral monitoring, rapid containment, and signed audit logs. **Partially aligned** — audit logging and rate limiting exist; behavioral/anomaly monitoring (flagging a change in an agent's activity pattern, not just a single denied call) does not exist as a feature yet. Real, undesigned gap. A narrower, more concrete sub-case of the same gap: some MCP gateways now differentiate specifically on schema-quarantine/tool-poisoning defenses and sensitive-data detection in tool *responses* (research-part-2.md, 2026-09-24) — folded into this same tracked gap rather than opened separately, since v0.1.0's explicit visibility-only scope already defers the whole category deliberately.

## Agent identity — cryptographic standards

- **DPoP (RFC 9449).** Sender-constrained credentials to prevent replay of a stolen credential. **Planned** — F-038, v0.2.0 credential broker.
- **OAuth Token Exchange `act` claim (RFC 8693).** The closest existing production mechanism for representing delegation identity (who a credential is acting on behalf of, through a chain). **Watching** — a possible v0.2.0+ interop shape if this platform ever needs to interoperate with another platform's delegation chains; not required for the self-hosted, single-tenant core, where the platform's own `parent_identity_id` already does this.
- **Decentralized Identifiers (DIDs) + Verifiable Credentials.** Cloud Security Alliance's recommended agentic identity model — a portable, independently verifiable identity anchor, not tied to one platform's database. **Not adopted, actively open** — see OQ-023. Real added complexity (key management, a resolution method, W3C DID spec compliance) for a portability problem a single-tenant self-hosted tool may not have yet. Revisit if a customer, or the broader MCP/A2A ecosystem, standardizes on DIDs before the identity schema locks further.
- **"Agent Identity Protocol" (AIP) — note on naming collision, 2026-09-24.** `research.md`'s Sept 21 entry already tracks one AIP ("Invocation-Bound Capability Tokens," a research proposal using Biscuit tokens for offline scope attenuation). A separate search this pass found at least four independently authored IETF individual drafts that also use the literal name "Agent Identity Protocol" for unrelated designs (`draft-aip-agent-identity-protocol`, `draft-prakash-aip`, `draft-singla-agent-identity-protocol`, `draft-fane-opena2a-aip`) — not confirmed to be the same effort renamed, and not reconciled against each other here; flagging the acronym collision itself so a future reader doesn't assume "the AIP draft" refers to one specific thing. None of the four is adopted as an IETF working-group item. **Watching, unchanged** — the fragmentation is evidence against committing to any of them yet, not a reason to move.
- **A2A (Agent2Agent protocol).** Confirmed via direct spec reading to have no delegation-identity, human-sponsor, or scope-narrowing mechanism of its own. **Not adopted** — validates rather than threatens this platform's identity model; nothing to interoperate with yet on this specific point.
- **WIMSE (Workload Identity in Multi-System Environments, IETF).** Aims to unify SPIFFE, OAuth, and JWT into one workload-identity foundation. **Watching** — still draft-stage, not broadly adopted.
- **SPIFFE-compatible workload identity.** Used for the platform's own zero-trust identity model (see `regulatory.md`, NIST AI RMF Agentic Profile mapping). **Adopted.**

## Instrumentation & observability

- **OpenTelemetry GenAI semantic conventions (`gen_ai.*` attribute names).** The fallback SDK path's wire format (F-024, gateway/otel_ingest.py's `POST /v1/otel/events`) reuses the standard's own attribute names (`gen_ai.operation.name`, `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.call.arguments`, `gen_ai.agent.name`, `error.type`) rather than a proprietary event shape, so a team that already runs an OTel collector recognizes the fields immediately. **Adopted, partially** — a small, honest subset targeted at exactly what F-024's fallback path needs (a completed tool call's identity, arguments, and outcome), not a full OTLP/protobuf exporter; nothing here would need to change shape if a real OTLP export path is added later, since the attribute names are the same either way.

## Build & supply chain

- **Sigstore / Cosign, keyless signing via CI OIDC identity.** Public releases signed in CI (GitHub Actions), signatures recorded in Sigstore's public transparency log (Rekor) — lets anyone verify they're running a genuine, unmodified build. **Adopted** — F-039, v0.1.0.
- **Component inventory / dependency pinning / signed manifests** (OWASP ASI04, Agentic Supply Chain). **Aligned** via the Sigstore decision above; a formal SBOM is not yet a committed feature.

## Governance frameworks tracked but not yet actionable

- **NIST agent identity & authorization initiative.** NIST's National Cybersecurity Center of Excellence published a concept paper (Feb 2026) soliciting industry input on agent identification, authorization, auditing, and non-repudiation — confirming this is an actively unsettled area industry-wide, not a solved problem this platform is behind on. Comment period closed April 2026; no resulting standard yet. **Watching.**
- **CSA "Securing the Agentic Control Plane."** Broader industry initiative (the CSA launched a dedicated foundation for this in March 2026) covering identity, governance, and assurance for agentic systems. **Watching** — source of the DID/VC recommendation above and a useful barometer for where the industry is converging.

## Why this document exists

Being open source and security-sensitive infrastructure (see `security.md`) means the design should be checked against outside standards, not just internal reasoning about what seems right. This is that check, kept current rather than done once and forgotten.
