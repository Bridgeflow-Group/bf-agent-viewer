# Status

The one place this repo's current build status gets updated. Every other doc that needs to say where the project stands links here instead of restating it — if you're editing a status line anywhere else, it probably belongs here instead.

## Where things stand

**v0.1.0 is building.** Real implementation started September 22, 2026, porting validated prototype logic into a tested, installable package.

**Built and passing tests:**

- Gateway core — per-request identity resolution (bearer-token based, not clientInfo-cached), delegation-scope enforcement (narrower-than-parent checked at registration and enforced at call time), per-agent rate limiting, tamper-evident event logging (hash-chained)
- Sandboxed backend process spawning — environment-variable allowlisting and POSIX resource-limit hardening, plus real filesystem/network isolation via a locked-down Docker container when Docker is available (falls back to the rlimit-only path with a logged warning otherwise, never silently)
- Test suite covering identity resolution, scope enforcement, rate limiting, event-chain integrity, sandboxing (both the rlimit and container paths, verified against real spawned processes), and a concurrent multi-identity integration test against a real running gateway process

**Not yet built:**

- Console / dashboard
- Alerting
- The production backend-sandbox container image (`docker/backend-sandbox/Dockerfile`) hasn't been built or tested on a host with normal registry access yet — only against a registry-free local substitute used for testing

For the version-by-version roadmap (v0.1.0 through v0.4.0) and what each future release is scoped to do, see [`versions.md`](versions.md). For the day-by-day research and validation log behind these decisions, see [`research.md`](research.md).
