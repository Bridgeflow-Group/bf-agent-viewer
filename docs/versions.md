# BF Agent Viewer

Version Tracker — living document

Started September 2026. Update status in place as versions move through design and release. Add new rows as future versions get scoped.


### Status legend

- proposed = scoped at a high level, not yet actively designed  |  in design = actively being specified  |  planned = design settled, queued for build  |  building = in development  |  released = shipped
| Version | Status | Focus | Description |
| --- | --- | --- | --- |
| v0.1.0 | in design | Know what your agents are doing | Visibility-first release: agent registry, event collection, activity dashboard, identity/owner/environment metadata, tool/resource activity history, search. No enforcement. Formerly "V1" in the design doc. This is the #1 focus and must not get diluted by compliance or enforcement framing. |
| v0.2.0 | proposed | Stop what's going wrong | Kill switch (credential-revocation based, sub-5-minute target) and credential/session lifecycle management. Formerly "V2." See OQ-004 in open-questions.docx for the kill-switch design decision. |
| v0.3.0 | proposed | Guide access | Permission wizard for least-privilege onboarding, plus the runtime policy engine (allow/deny based on identity, resource, action, context). Formerly "V3." |
| v0.4.0 | proposed | Preserve accountability | Delegation and human-approval flows, keeping an accountable human context attached to agent actions even when work is delegated agent-to-agent. Formerly "V4." |
