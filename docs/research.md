# Research & Best Practices Log

A living log of the standards, industry research, and technical findings behind BF Agent Viewer's design. Published so anyone reading the docs can tell us if we're missing something -- open an issue or reply if you see a gap here.

This isn't a changelog of the product (see [`versions.md`](versions.md) for that) -- it's a record of *why* certain design choices were made, and what's still being watched.

---

## 2026-09-21

**Industry convergence on five shared primitives.** A landscape report on AI agent identity platforms identifies convergence across vendors around five primitives: an agent registry, a human sponsor/owner bound to each agent, short-lived scoped credentials, MCP-layer enforcement, and runtime (not login-time) authorization. v0.1.0 covers four of five by design -- runtime authorization is deliberately deferred (visibility comes before enforcement, see [`positioning.md`](positioning.md)), not an oversight.

**MCP went stateless -- July 28, 2026 spec update.** The Model Context Protocol removed its handshake/session-based model in favor of stateless per-request identity and capabilities, and hardened its OAuth integration (RFC 9207 issuer validation, Client ID Metadata Documents replacing Dynamic Client Registration). This matters here because the MCP-gateway instrumentation approach (see [`how-it-works.md`](how-it-works.md)) was designed around MCP having something like a session boundary. Under review: whether "session" in this design still maps to anything MCP itself exposes post-update, or whether the gateway needs to construct its own session boundary.

**WIMSE -- watchlist.** An IETF working group effort (Workload Identity in Multi-System Environments) aims to tie SPIFFE, OAuth, and JWT together into one workload-identity foundation. Complements SPIFFE rather than replacing it. Still at draft stage, not yet broadly adopted -- watching, not acting yet.

**A real open-source competitor showed up.** Microsoft open-sourced an "Agent Governance Toolkit" (MIT license) in April 2026 -- a seven-package stack (policy engine, cryptographic inter-agent identity, execution sandboxing, SRE practices, compliance verification, plugin marketplace, RL training governance) aimed at complex, multi-agent production environments. It's real competition and a reminder that "open source" alone isn't a differentiator -- but it's built for teams with the infrastructure investment to run seven packages, not the buyer this project targets (see [`positioning.md`](positioning.md)).

**Gap found and closed: no alerting.** The feature set was entirely passive-dashboard -- nothing pushed information to anyone. Since the target buyer has no dedicated security team watching a dashboard all day, that's a real gap for something like a newly-discovered, unclaimed agent sitting unnoticed. Basic email/webhook notification is now on the v0.1.0 feature list.

---

*Sources and further detail for each entry are tracked internally; if you want a citation for something above, ask in an issue and it'll get added.*
