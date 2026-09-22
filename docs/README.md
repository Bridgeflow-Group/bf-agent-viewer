# BF Agent Viewer

**Know what your agents are doing.**

An open-source, self-hosted identity and visibility layer for AI agents. Most teams running agents right now have no clear picture of which agent talked to which tool, using whose credentials, when. BF Agent Viewer answers that question first, before anything about control or enforcement.

## Status

See [`status.md`](status.md) — the one place build status is kept up to date.

## Why visibility first

A kill switch without a clear audit trail tells you *that* something went wrong, not *which* tool call crossed the line. So v0.1.0 does one thing: agent registry, activity/event logging, identity and ownership metadata, search. No policy enforcement, no kill switch — those come later, once there's something real to enforce against. See [`versions.md`](versions.md) for the full roadmap.

## Who this is for

Teams running AI agents without a dedicated security or identity-governance function. This isn't a lighter version of enterprise agent-security tooling — it's built for a buyer those tools don't serve well. See [`positioning.md`](positioning.md) for the full reasoning, including the honest caveat about what "open source" does and doesn't guarantee as a differentiator.

## License and cost

Apache 2.0. Self-hosted deployments are never capped by agent count — run it against 5 agents or 5,000, free, no license check, no sales call. See [`licensing-model.md`](licensing-model.md) for how the (future) Paid/Enterprise tiers are scoped without touching that.

## Docs in this repo

- [`status.md`](status.md) — current build status, the one place it's kept up to date
- [`positioning.md`](positioning.md) — how this differs from existing agent identity/security tools, and why
- [`market.md`](market.md) — the market this is built for
- [`versions.md`](versions.md) — the version-by-version roadmap (v0.1.0 through v0.4.0)
- [`licensing-model.md`](licensing-model.md) — the Free/Paid/Enterprise model
- [`how-it-works.md`](how-it-works.md) — how registration, instrumentation, identity, delegation, and (eventually) the kill switch actually work
- [`security.md`](security.md) — the platform's own security architecture and what it does and doesn't do yet
- [`standards.md`](standards.md) — technical and security standards this design follows, tracks, or has deliberately not adopted, and why
- [`regulatory.md`](regulatory.md) — the legal and compliance landscape (EU AI Act and similar)
- [`research.md`](research.md) — the running research log: validation results, design decisions, and findings from building against real traffic

## Get involved

The design is public and the first real code is landing now. If this is a problem you've run into, or you want to help shape the design, open an issue or start a discussion — feedback is useful at this stage whether it's about the architecture or the code.
