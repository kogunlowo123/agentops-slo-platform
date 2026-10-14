# ADR-0004: Policy-gated recovery actions published as directives

- Status: Accepted
- Date: 2026-10-14

## Context

Recovery advice only helps if something applies it, but a platform that
reconfigures production traffic on its own is a new failure mode and a
security risk.

## Decision

- Recommendations (retry policy, circuit state) are computed, never applied.
- Applying one is a *recovery action* with a fixed risk per type (retry
  policy LOW, circuit close MODERATE, circuit open HIGH, budget raise HIGH,
  agent pause CRITICAL), evaluated by the agentic-core policy engine against
  `policies/recovery-actions.yaml`.
- `allow` makes the action active for its TTL; `require_approval` waits for
  an SRE who is not the proposer, with compare-and-set on status so two
  approvers cannot both decide; `deny` is recorded.
- Active actions are published at `GET /v1/recovery/directives`. Agent
  runtimes and gateways pull and apply them. Directives expire on their own,
  and pending actions lapse if nobody decides within the TTL.
- Every proposal and decision is audited with the policy digest.

## Consequences

- The platform needs no write access to any agent runtime.
- Applying directives is the runtime's job; a runtime that does not poll
  gets no protection, which rollout reviews must check.
- Expiry makes forgotten directives harmless.
