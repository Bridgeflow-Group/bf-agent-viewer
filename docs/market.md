# Why This, Why Now

The market data behind BF Agent Viewer's thesis. Figures below are vendor/analyst survey data, not independently audited statistics — they're directional evidence of urgency, not precise measurements. Compiled September 2026.

## Adoption is outpacing control

- 81% of teams have moved past planning into testing or production for AI agents, but only 14.4% report full security/IT approval for all agents going live.
- Organizations actively monitor an average of 47.1% of their agent fleet — over half of deployed agents operate with no active oversight.
- 82% of executives feel confident their policies protect against unauthorized agent actions, while over half of all agents run without security oversight or logging at all. That confidence/reality gap is the problem this project exists to close.

[Gravitee, "State of AI Agent Security 2026: When Adoption Outpaces Control"](https://www.gravitee.io/blog/state-of-ai-agent-security-2026-report-when-adoption-outpaces-control)

## Incidents are already happening

- 88% of organizations confirmed or suspected an AI agent security incident in the past year; a separate survey puts it at 65%, with 61% of those involving data exposure and 41% causing unintended business-process actions.
- 63% of organizations cannot enforce purpose limitations on agents, and 60% lack the capability to terminate a misbehaving agent.
- Only 19% of organizations classify AI agents as equivalent to human insiders for governance purposes — despite documented cases of agents gaining unauthorized database write access and attempting data exfiltration.

[Gravitee, "State of AI Agent Security 2026"](https://www.gravitee.io/blog/state-of-ai-agent-security-2026-report-when-adoption-outpaces-control) · [Kiteworks, "AI Agent Security Incidents Hit 65% of Firms in 2026"](https://www.kiteworks.com/cybersecurity-risk-management/ai-agent-security-incidents-2026/)

## The identity governance vacuum

- Only 21.9% of organizations treat AI agents as independent, identity-bearing entities; 45.6% still rely on shared API keys for agent-to-agent authentication, and 27.2% use custom hardcoded authorization logic.
- 25.5% of deployed agents can create and task other agents — delegation isn't a hypothetical edge case, it's already common practice.
- Non-human identities outnumber human users 45:1 on average (144:1 in cloud-native environments), yet only 15% of organizations feel confident preventing attacks based on non-human identity.
- 51% of organizations report no clear ownership of AI identities, and only 20% have formal offboarding processes for API keys and credentials.
- 78% of organizations lack documented policies for managing AI identities, and more than 16% don't even track new AI-related identity creation. Visibility has to come before policy — which is exactly why this project's first release does visibility only, nothing else.

[Cloud Security Alliance, "The Non-Human Identity Governance Vacuum"](https://labs.cloudsecurityalliance.org/research/csa-whitepaper-nonhuman-identity-agentic-ai-governance-v1-cs/)

## What analysts expect

Gartner predicts 40% of enterprises will demote or decommission autonomous AI agents by 2027 due to governance failures discovered after production incidents, and recommends proportional governance across four autonomy levels (Observe, Advise, Act-with-Approval, Act-Autonomously) rather than one-size-fits-all controls — the same shape independently reflected in NIST's AI Risk Management Framework Agentic Profile.

[Gartner, "Applying Uniform Governance Across AI Agents Will Lead to Enterprise AI Agent Failure" (May 2026)](https://www.gartner.com/en/newsroom/press-releases/2026-05-26-gartner-says-applying-uniform-governance-across-ai-agents-will-lead-to-enterprise-ai-agent-failure)

## Regulatory pressure is real, not speculative

The EU AI Act's Article 14 mandates human oversight, including a "stop" capability, for in-scope systems — turning a kill switch from a feature request into a compliance requirement in EU markets. Buyer-side research reports target windows of under 5 minutes to terminate a production agent, and under 1 minute for agents with transaction authority.

[EU AI Act, Article 14 — Human Oversight](https://artificialintelligenceact.eu/article/14/) · [Zylos Research, "Buyer-Side Governance: What Enterprise Customers Now Demand From AI Agent Vendors" (Jul 2026)](https://zylos.ai/research/2026-07-02-buyer-side-governance-enterprise-ai-agent-deployments/) · [Forbes, "1.3 Billion AI Agents Are Coming. Most Don't Have A Kill Switch" (Mar 2026)](https://www.forbes.com/sites/terdawn-deboe/2026/03/23/13-billion-ai-agents-are-coming-most-have-no-kill-switch/) · [Pebblous, "Why Enterprises Can't Stop a Runaway AI Agent"](https://blog.pebblous.ai/blog/agent-kill-switch-observability/en/)

## What this adds up to

Every gap above — the confidence/reality mismatch, the shared-credential mess, the missing ownership, the lack of a kill switch — is a gap in *knowing what's happening*, not just a gap in *stopping it*. That's why v0.1.0 is visibility, full stop, before anything about control. See [`positioning.md`](positioning.md) for how that shapes the product, and [`how-it-works.md`](how-it-works.md) for what v0.1.0 actually does.
