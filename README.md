# BF Agent Viewer

**Know what your agents are doing.**

An open-source, self-hosted identity and visibility layer for AI agents. Most teams running agents right now have no clear picture of which agent talked to which tool, using whose credentials, when. BF Agent Viewer answers that question first, before anything about control or enforcement.

## Status

See [`docs/status.md`](docs/status.md) — the one place build status is kept up to date.

## Quickstart

```
git clone https://github.com/Bridgeflow-Group/bf-agent-viewer.git
cd bf-agent-viewer
docker compose up -d --build
```

That starts the gateway (`:8941`) and the read-only console (`:8942`), proxying a small bundled example MCP server (`examples/demo_backend.py`) so there's something real to look at immediately. Point `BF_BACKEND_SCRIPT` (in `docker-compose.yml`) at your own MCP server when you're ready to move past the example.

First run needs one-time bootstrapping — an organization, a human owner, an agent, and a console login:

```
docker compose run --rm gateway bf-agent-viewer org create --id org-1 --name "Your Org"
docker compose run --rm gateway bf-agent-viewer human create --id human-1 --name "Your Name" --email you@example.com
docker compose run --rm gateway bf-agent-viewer register --agent-id agent-1 --name "First Agent" --owner human-1 --scope get_weather send_email
docker compose run --rm gateway bf-agent-viewer console-user create --human human-1
```

The last command prints a secret and QR-style URI — add it to an authenticator app (Google Authenticator, 1Password, etc.), then log in at `http://localhost:8942` and enter the code to finish setup (console login is mandatory MFA, no password-only path — see [`docs/security.md`](docs/security.md)). Use the `register` command's printed token as the `X-BF-Agent-Token` header on requests to `http://localhost:8941/mcp`.

Prefer running without Docker? `pip install -e ".[console]"` and the same `bf-agent-viewer <command>` invocations work directly — `docker-compose.yml` is a thin wrapper around them, not a separate mechanism. Note: the compose deployment runs backend sandboxing in rlimit-only mode rather than the container-isolated path (see the comment at the top of `docker-compose.yml` for why); a direct host install gets the full container-sandboxed path when Docker is available on that host. See [`docs/CLI.md`](docs/CLI.md) for every command's flags and env vars.

## Why visibility first

A kill switch without a clear audit trail tells you *that* something went wrong, not *which* tool call crossed the line. So v0.1.0 does one thing: agent registry, activity/event logging, identity and ownership metadata, search. No policy enforcement, no kill switch — those come later, once there's something real to enforce against. See [`docs/versions.md`](docs/versions.md) for the full roadmap.

## Who this is for

Teams running AI agents without a dedicated security or identity-governance function. This isn't a lighter version of enterprise agent-security tooling — it's built for a buyer those tools don't serve well. See [`docs/positioning.md`](docs/positioning.md) for the full reasoning, including the honest caveat about what "open source" does and doesn't guarantee as a differentiator.

## License and cost

Apache 2.0. Self-hosted deployments are never capped by agent count — run it against 5 agents or 5,000, free, no license check, no sales call. See [`docs/licensing-model.md`](docs/licensing-model.md) for how the (future) Paid/Enterprise tiers are scoped without touching that.

## Docs in this repo

- [`docs/status.md`](docs/status.md) — current build status, the one place it's kept up to date
- [`docs/CLI.md`](docs/CLI.md) — every `bf-agent-viewer` command, its flags and env vars, and a complete first-run walkthrough
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

If this is a problem you've run into, or you want to help shape the design, open an issue or start a discussion. Early feedback — on the architecture and on the real build, see [`docs/status.md`](docs/status.md) — is exactly what this stage of the project needs.
