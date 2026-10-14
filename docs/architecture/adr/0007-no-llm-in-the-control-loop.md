# ADR-0007: No model calls in the control loop

- Status: Accepted
- Date: 2026-10-14

## Context

It is tempting to let a model summarize incidents or pick recovery actions.
The platform's outputs drive paging, budgets and changes to production
behaviour, so they must be reproducible and auditable.

## Decision

- The platform makes no model calls. Classification, recommendations, A2A
  routing and summaries are deterministic code.
- The agentic-core `ModelRouter` and a `configs/providers.yaml` are therefore
  intentionally absent.
- agentic-core controls are used where they apply: `SecretScanner` and
  `PiiDetector` scrub error text before storage, `PolicyEngine` decides
  access and recovery, and `AuditLog` records decisions.

## Consequences

- Telemetry content has no path into decisions; it is treated as data and
  most of it is dropped at decode time.
- The platform has no model spend and no provider dependency.
- Narrative incident summaries are left to the calling agent, which can
  combine our structured results with its own model under its own controls.
