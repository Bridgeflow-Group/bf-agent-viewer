# Competitive Positioning

BF Agent Viewer -- living document. Confirmed positioning as of September 18, 2026. See research doc (competitive landscape) and regulatory-requirements.docx for the underlying evidence.


## 1. The headline

"Know what your agents are doing" -- for teams that don't have a security function to hand this to. That is the #1 focus and it is not a smaller version of what NewCore, Astrix, and Aembit sell; it's a different buyer.


## 2. Three points of separation from NewCore / Astrix / Aembit


### Built for a buyer without a security function

NewCore, Astrix, and Aembit all assume the customer already has someone whose job is identity/security governance -- their pitch is 'manage this well,' which presupposes active management already exists. BF Agent Viewer's target user has no one in that role. This isn't a lighter-weight version of the same product for a smaller company; the incumbents can't serve this buyer without becoming a different kind of product themselves.


### Visibility first, not control first

All three incumbents lead with policy enforcement, credential brokering, and access governance as their core value. v0.1.0 deliberately does none of that. This is a defensible sequencing choice, not a feature gap: research (Pebblous, cited in research doc section 11.7) found that a kill switch without identity/event lineage is 'a shot in the dark.' A buyer with no governance maturity isn't well served by control they can't operate yet -- visibility has to come first for this buyer specifically.


### Self-hosted and uncapped vs. enterprise-priced

Download it and run it today, free, no sales call, no agent-count cap (see licensing-model.docx). None of the three incumbents are built to compete on this. This is a real structural moat for as long as it holds.


## 3. The honest caveat

"Open source" alone is not a differentiator anymore -- plenty of security tooling is open source. The differentiator is the combination: open source, zero enterprise sales motion, and visibility-first sequencing, aimed at a buyer the incumbents structurally can't serve. And that moat has a shelf life -- the research doc's own competitive-risk finding (section 1) is that a funded vendor could move downmarket with a free tier. This positioning holds only as long as that hasn't happened.

## 4. Landscape updates (addenda, most recent first)

**2026-09-24.** Two developments from an industry-alignment research pass (see `research-part-2.md`), neither requiring a positioning change yet, both worth tracking against the shelf-life risk in section 3:

- **Astrix Security was acquired by Cisco in May 2026.** One of the three named incumbents above is now backed by a platform vendor's distribution and resources rather than operating as an independent company -- exactly the kind of consolidation that could eventually fund a downmarket move, though nothing found this pass indicates Cisco/Astrix has actually done that yet. Astrix's own positioning (discovery/governance, not runtime enforcement) is unchanged; only its backing is.
- **Pomerium is a new named entrant worth tracking, not yet a fourth incumbent to position against.** An open-core identity-aware gateway with MCP-specific tool-level authorization, positioned closer to this platform's own "no vendor lock-in" self-hosted angle than NewCore/Astrix/Aembit are -- but Pomerium's buyer already runs a Zero Trust program (it extends one), which is a different buyer than the "no security function at all" target this platform is built for. Section 2's "built for a buyer without a security function" distinction likely still holds against Pomerium specifically, but this wasn't tested directly and shouldn't be assumed indefinitely.
