# Security & Architecture

How BF Agent Viewer is built, and — just as important — what it does and doesn't do yet. This is a design document, not an audit; see [`status.md`](status.md) for what's actually built right now.

## Why this matters now

MCP, the primary instrumentation path this platform relies on (see [`how-it-works.md`](how-it-works.md)), has a real, current vulnerability landscape: 14 publicly assigned CVEs in 2026 plus 30+ further RCE issues traced to the same root cause in the official SDKs, roughly 7,000 MCP servers confirmed publicly reachable via active scanning, and -- across a sample of 2,614 real implementations -- 82% vulnerable to path traversal, 67% using code-injection-prone APIs, and 34% susceptible to command injection. None of that is a reason to avoid MCP; it's the reason this platform treats the gateway itself as security-sensitive infrastructure rather than a thin pass-through, and why the principles below aren't boilerplate.

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

**Console authentication.** TOTP-based MFA plus standard session hardening (httpOnly/secure/SameSite cookies, CSRF protection, session rotation and revocation) via a proven library, not custom session/crypto code -- these are exactly the primitives that are easy to get subtly wrong when hand-rolled. An optional lightweight reverse-proxy auth gate is a supported pattern for anyone who wants SSO in front of the console; it isn't required for a single-tenant self-hosted install.

**Per-agent rate limiting.** Each agent gets its own token-bucket allocation (steady rate plus burst) at the gateway. A single runaway or misbehaving agent can only exhaust its own allocation -- it can't degrade visibility for the rest of the fleet by flooding the shared event pipeline. Calls beyond an agent's allocation are rejected at the gateway, not silently queued, and a rejection itself is surfaced as an alert.

**Gateway resilience.** Since v0.1.0 does zero enforcement, the gateway's only job is capturing events, not gating them -- so it fails open by default: if the gateway is down or unreachable, agent traffic passes through unlogged rather than being blocked. That gap is never silent -- the gateway writes an explicit gap-marker event (start/end of the outage) when it comes back up, and fires an alert. One limitation this doesn't solve: tamper-evidence protects history that already happened, but can't stop a *live*, compromised gateway process from forging new, correctly-chained events going forward, since a compromised process has the same signing capability a legitimate one would. The only real mitigation is keeping the gateway's own attack surface small, single-purpose, and least-privilege -- named here rather than implied away.

**Verifying your build.** Public releases are signed in CI via Sigstore/Cosign using GitHub Actions' own OIDC identity -- no keys for anyone to manage -- with signatures recorded in Sigstore's public transparency log (Rekor). That lets you verify you're running a genuine, unmodified build before trusting this as security infrastructure, rather than taking it on faith.

## What v0.1.0 explicitly does not do

- No policy enforcement or action-blocking. That's the difference between visibility and control, and control comes later.
- No kill switch. Stopping a misbehaving agent is a deliberate v0.2.0 capability (credential revocation, not real-time interception), not part of the first release. That v0.2.0 credential broker is designed to be a separate, isolated service with its own signing key and a bounded ability to mint credentials (scope ceilings, per-agent rate limits, short key rotation), so a broker compromise is bounded rather than total -- and broker-issued credentials will be sender-constrained (DPoP) so a stolen credential alone isn't enough to use it.
- No multi-tenant isolation enforcement. Single-tenant self-hosted doesn't need it; a future hosted offering would.
- Coverage gap by design: an agent calling APIs directly, with no MCP layer and no SDK integration, isn't visible to the platform yet. See [`how-it-works.md`](how-it-works.md) for the instrumentation approach and its known boundary.

## Why this order

A kill switch without a clear record of what happened tells you *that* something went wrong, not *which* action crossed the line. Building control before visibility is how you end up with a system you can't actually operate — so this is built visibility-first, deliberately, even though it means v0.1.0 can't stop anything yet.
