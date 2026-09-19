# Licensing & Pricing Model

BF Agent Viewer -- living document. See features.xlsx (License Tier column) for the authoritative feature-to-tier mapping; this doc explains the model and principle behind it.


## 1. The core decision

Tiers are feature-based, not usage-based. Self-hosted deployments are never capped by agent count -- run 5 agents or 5,000 on the free, self-hosted tier, no license check, no phone-home. This was a deliberate choice, not an oversight: this platform's whole thesis is 'open-source core and self-hosted first' (design doc section 2), targeted at SMB teams with no dedicated security admin. A usage cap enforced on self-hosted software requires some kind of license-verification mechanism, which is exactly the kind of operational friction and admin burden this product exists to avoid -- and it's trivially defeatable anyway, since the code is open source and the check can be read and patched out.


## 2. The three tiers


### Free (self-hosted)

Everything needed to actually run this product and get its core value -- agent registry, event collection, dashboard, search, identity/owner/environment metadata, instrumentation (MCP gateway and OpenTelemetry SDK), the kill switch, credential lifecycle management, the basic runtime policy engine, and single-binary/SQLite deployment. No agent-count limit. This is the bulk of features.xlsx -- v0.1.0's entire feature set is free, and most of v0.2.0/v0.3.0 stays free too.


### Paid

Advanced capabilities that go beyond core visibility/control but aren't required to run the product safely: SIEM export/streaming, behavioral anomaly telemetry, extended/managed log retention beyond the default window, and premium integrations plus paid support SLAs. These can run self-hosted with a license key gating the specific feature, or via the hosted offering.


### Enterprise

Organizational-scale governance: SSO/SCIM administration, managed multi-tenant hosting, enterprise policy packs (pre-built templates for the policy engine, some aligned to specific regulatory-requirements.docx items), and the fully hosted/managed control plane for teams that don't want to self-host at all.


## 3. Where a hosted SaaS offering fits

If a hosted version of this platform is ever built (we run the collector/event store/dashboard instead of the customer), usage-based tiering (agent count, event volume) belongs there -- not on the self-hosted software. The self-hosted free tier stays uncapped regardless of whether a hosted offering exists alongside it.


## 4. Open items

- Exact pricing (dollar amounts, thresholds) is not yet decided -- this doc defines the shape of the model, not the numbers.
- License-key enforcement mechanism for self-hosted Paid features (e.g. a signed key file checked at startup) is not yet designed.
- Whether F-012 (runtime policy engine) and F-015 (org/tenant isolation) split cleanly at the boundaries described here should be revisited once the v0.3.0 policy engine is actually specified.