# BF Agent Viewer

Version Tracker — living document

Started September 2026. Update status in place as versions move through design and release. Add new rows as future versions get scoped. For what's actually built right now, see [`status.md`](status.md) — that's the one place current build status is kept up to date, not this table.


### Status legend

- proposed = scoped at a high level, not yet actively designed  |  in design = actively being specified  |  planned = design settled, queued for build  |  building = in development  |  released = shipped

| Version | Status | Focus | Description |
| --- | --- | --- | --- |
| v0.1.0 | building | Know what your agents are doing | Visibility-first release: agent registry, event collection, activity dashboard, identity/owner/environment metadata, tool/resource activity history, search. No enforcement. Formerly "V1" in the design doc. This is the #1 focus and must not get diluted by compliance or enforcement framing. Real implementation started Sept 22, 2026; every v0.1.0-scoped task is now `done`, including T-013 (packaging the app as a single runnable container image) — closed 2026-09-26 via a CI job that builds and smoke-tests both container images on a GitHub-hosted runner with real registry access. Kill switch (credential revocation, F-009) and short-lived credentials (F-016) — originally scoped for v0.2.0 below — were pulled forward into v0.1.0 once T-022/T-024 turned out to need each other anyway. Status stays `building` here until the actual `v0.1.0` git tag is cut; see [`status.md`](status.md) for current build status. |
| v0.2.0 | proposed | Deepen credential and session control | Credential/session lifecycle management beyond basic revocation, a credential-broker service with its own isolated signing key and bounded issuance, DPoP-bound credentials (replay protection), SIEM export/streaming, and behavioral anomaly telemetry. Formerly "V2." The kill switch itself (credential revocation) and short-lived credentials shipped early, in v0.1.0 — see the row above; this scope is what's left once those moved. |
| v0.3.0 | proposed | Guide access | Permission wizard for least-privilege onboarding, plus the runtime policy engine (allow/deny based on identity, resource, action, context). Formerly "V3." |
| v0.4.0 | proposed | Preserve accountability | Delegation and human-approval flows, keeping an accountable human context attached to agent actions even when work is delegated agent-to-agent. Formerly "V4." |
