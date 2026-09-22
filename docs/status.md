# Status

The one place this repo's current build status gets updated. Every other doc that needs to say where the project stands links here instead of restating it — if you're editing a status line anywhere else, it probably belongs here instead.

## Where things stand

**v0.1.0 is building.** Real implementation started September 22, 2026, porting validated prototype logic into a tested, installable package.

**Built and passing tests:**

- Gateway core — per-request identity resolution (bearer-token based, not clientInfo-cached), delegation-scope enforcement (narrower-than-parent checked at registration and enforced at call time), per-agent rate limiting, tamper-evident event logging (hash-chained)
- Sandboxed backend process spawning — environment-variable allowlisting and POSIX resource-limit hardening, plus real filesystem/network isolation via a locked-down Docker container when Docker is available (falls back to the rlimit-only path with a logged warning otherwise, never silently)
- Console — read-only dashboard, agent detail (identity, delegation chain, granted scope), event detail, search, onboarding empty-state. Server-rendered (Starlette + Jinja2), no new web-framework dependencies beyond what the gateway already pulls in
- Console authentication (F-040) — password + mandatory TOTP MFA (`pyotp`), hardened sessions (hashed tokens, 12h expiry, httpOnly/SameSite=Strict cookies), lockout after 5 failed passwords. No console route is reachable without a valid session; first login forces MFA enrollment before any session is issued. `bf-agent-viewer console-user create` provisions the first login. See [`security.md`](security.md) for the mechanism
- CLI: `org create` / `human create` (the bootstrap path a fresh database needs — `register` and `console-user create` both assume these rows already exist), `gateway` (runs the real gateway as a persistent HTTP process — previously only exercised inside tests, now a real command), `console`, `console-user create`, `register`. All env-var-configurable (`BF_DB`, `BF_ORG`, etc.) alongside CLI flags, for container use
- Docker Compose self-hosted deployment (F-018) — `docker compose up` starts the gateway and console from one image (`docker/app/Dockerfile`), sharing a SQLite volume, proxying a bundled example backend (`examples/demo_backend.py`) out of the box. Verified end-to-end against the real runtime (org→human→agent→gateway call→console login), but the actual `docker build` itself is unverified here — same registry-access limitation as the backend-sandbox image below. Runs backend sandboxing in rlimit-only mode by design (mounting the host's Docker socket into the gateway's own container to get the container-isolated path would undermine the isolation it's meant to provide — see `docker-compose.yml`'s own comment and security.md)
- Test suite covering identity resolution, scope enforcement, rate limiting, event-chain integrity, sandboxing (both the rlimit and container paths, verified against real spawned processes), the console and its auth flow (real requests against a seeded database, driven through the actual login/enroll/verify HTTP routes, not a bypass), the new CLI commands (real invocations against a real SQLite file, not mocks), and a concurrent multi-identity integration test against a real running gateway process

**Not yet built:**

- Alerting
- The production backend-sandbox container image (`docker/backend-sandbox/Dockerfile`) and the app image (`docker/app/Dockerfile`) haven't been built or tested on a host with normal registry access yet — only against a registry-free local substitute used for testing

For the version-by-version roadmap (v0.1.0 through v0.4.0) and what each future release is scoped to do, see [`versions.md`](versions.md). For the day-by-day research and validation log behind these decisions, see [`research.md`](research.md).
