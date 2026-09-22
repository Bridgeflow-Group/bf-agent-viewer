# BF Agent Viewer

**Know what your agents are doing.**

An open-source, self-hosted identity and visibility layer for AI agents. Most teams running agents right now have no clear picture of which agent talked to which tool, using whose credentials, when. BF Agent Viewer answers that question first, before anything about control or enforcement.

## Status

**Design phase. Nothing is built yet.** This repo currently holds the design documents and roadmap, published in the open so the project is transparent from day one and so early feedback can shape it before code exists.

## Why visibility first

A kill switch without a clear audit trail tells you *that* something went wrong, not *which* tool call crossed the line. So v0.1.0 does one thing: agent registry, activity/event logging, identity and ownership metadata, search. No policy enforcement, no kill switch — those come later, once there's something real to enforce against. See [`docs/versions.md`](docs/versions.md) for the full roadmap.

## Who this is for

Teams running AI agents without a dedicated security or identity-governance function. This isn't a lighter version of enterprise agent-security tooling — it's built for a buyer those tools don't serve well. See [`docs/positioning.md`](docs/positioning.md) for the full reasoning, including the honest caveat about what "open source" does and doesn't guarantee as a differentiator.

## License and cost

Apache 2.0. Self-hosted deployments are never capped by agent count — run it against 5 agents or 5,000, free, no license check, no sales call. See [`docs/licensing-model.md`](docs/licensing-model.md) for how the (future) Paid/Enterprise tiers are scoped without touching that.

## Docs in this repo

- [`docs/positioning.md`](docs/positioning.md) — how this differs from existing agent identity/security tools, and why
- [`docs/versions.md`](docs/versions.md) — the version-by-version roadmap (v0.1.0 through v0.4.0)
- [`docs/licensing-model.md`](docs/licensing-model.md) — the Free/Paid/Enterprise model
- [`docs/how-it-works.md`](docs/how-it-works.md) — how registration, instrumentation, identity, delegation, and (eventually) the kill switch actually work
- [`docs/market.md`](docs/market.md) — the market data behind the thesis: adoption, incidents, the identity governance gap, regulatory pressure
- [`docs/security.md`](docs/security.md) — the security model and architecture, including an honest list of what v0.1.0 doesn't do yet
- [`docs/regulatory.md`](docs/regulatory.md) — how the design maps to the EU AI Act, NIST AI RMF, and other current frameworks
- [`docs/standards.md`](docs/standards.md) — which technical/security standards (MCP security best practices, OWASP Agentic Top 10, DPoP, Sigstore, and more) this design follows, tracks, or has deliberately not adopted, and why
- [`docs/research.md`](docs/research.md) — running log of research and industry standards behind these decisions (and where to tell us if we're missing something)

## Get involved

If this is a problem you've run into, or you want to help shape the design before any code is written, open an issue or start a discussion. Early feedback on the architecture is exactly what this stage of the project needs.
