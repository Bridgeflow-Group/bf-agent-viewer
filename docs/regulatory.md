# Regulatory Alignment

How BF Agent Viewer's design maps to current AI agent regulation and frameworks. This isn't legal advice, and running this software doesn't make you compliant with anything on its own — it's meant to show that the design was built with real regulatory requirements in view, not guessed at.

## EU AI Act

- **Article 12 / Annex III — automatic logging.** High-risk AI systems must automatically log events over their lifetime. This only applies when an agent's use case is legally "high-risk" (credit, hiring, healthcare, insurance, emergency triage, and similar) — not every SMB agent use case qualifies. Covered by the agent registry and event collection.
- **Article 19 / 26 — retention.** A minimum 6-month log retention period applies to high-risk system logs. v0.1.0 supports a configurable retention policy with a compliance-safe default.
- **Tamper-resistance.** Standard mutable logs aren't sufficient as evidence for regulators. Event records are tamper-evident by design — see [`security.md`](security.md).
- **Article 14 — human oversight.** Requires a "stop" capability for in-scope systems. This is the v0.2.0 kill switch, built on credential revocation.

## NIST AI Risk Management Framework — Agentic Profile

- **Accountability register.** Every agent needs a business owner, a technical owner, delegation lineage, and review conditions. v0.1.0 covers owner and technical-owner fields and full delegation lineage; a formal recurring-review workflow is an Enterprise-tier capability, not core.
- **Dynamic agent registry integrated with IAM.** Covered by the core registry; SSO/SCIM-style IAM integration is a later-stage, commercial-layer addition, not part of the self-hosted core.
- **Verifiable, zero-trust agent identity.** The design uses SPIFFE-compatible workload identity to prevent impersonation and delegation-chain injection.
- **Behavioral telemetry.** Action velocity, permission-escalation frequency, cross-boundary invocations, delegation depth, and exception rates, with baseline-deviation flagging — a defined, Paid-tier capability.
- **Four-tier autonomy classification** (Observe / Advise / Act-with-Approval / Act-Autonomously). NIST's framework and Gartner's independent research land on the same four-tier shape — a strong signal this is close to a de facto standard, not one vendor's opinion. v0.1.0 tracks this as metadata; enforcement arrives with the runtime policy engine.
- **Pre-authorized automatic containment** for highest-severity incidents, since human-in-the-loop response is often too slow. This is why the kill switch is built on credential revocation the platform can trigger itself, not a workflow requiring a human to act in real time.

## Federal Executive Order / FedRAMP guidance

- **Scoped, short-lived credentials**, replacing long-lived static keys and standing access, issued only for what an agent needs.
- **Complete audit trail** for every access decision — tool calls, authorization decisions, access attempts — exportable for oversight reporting.
- **Every agent as a known, owned, first-class identity** with a clear accountable human owner. This is the core thesis of the product, not an add-on for this requirement specifically.

## US state laws (consequential-decision transparency)

Colorado SB 26-189, California AB 2013/SB53/SB942, Texas TRAIGA, NYC Local Law 144, and Connecticut PA 26-15/26-100 require documentation, notice, human-review rights, and bias audits for automated systems making consequential decisions about people. This targets decision-transparency to the *people affected by* an agent's decisions — a different layer than agent identity and visibility, and only relevant if a customer's agent makes consequential decisions about people. BF Agent Viewer doesn't solve explainability itself, but makes it straightforward to export the audit trail data a customer would need for their own compliance obligations.
