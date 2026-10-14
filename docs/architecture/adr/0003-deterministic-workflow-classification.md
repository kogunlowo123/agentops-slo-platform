# ADR-0003: Deterministic workflow outcome classification

- Status: Accepted
- Date: 2026-10-14

## Context

A trace is one workflow run. Teams need to know whether it succeeded and, if
not, why and what to do. The diagnosis feeds SLIs and recovery, so it must be
reproducible and explainable to an auditor.

## Decision

- A run is finalized when its root span has arrived and no span has arrived
  for `AGENTOPS_WORKFLOW_QUIET_S` (30 s), or when nothing has arrived for
  `AGENTOPS_WORKFLOW_MAX_WAIT_S` (15 min) without a root, which yields
  `incomplete`.
- Outcome: `success` (no failed spans), `recovered` (failed spans but the
  root succeeded), `failed` (root failed, or one tool was called more than
  `AGENTOPS_LOOP_THRESHOLD` times under the same parent), `incomplete`.
- Root cause: the failed span with no failed descendants, earliest end on
  ties. The failure class comes from ordered rules over finish reasons,
  error type and message, HTTP status and span kind: guardrail, rate limit,
  timeout, authorization, budget, loop, then tool, model, memory or agent
  error. Each class maps to fixed recovery strategies and a retryable flag.
- Finalized runs are not reopened by late spans.

## Consequences

- The same trace always yields the same diagnosis, and tests pin each rule.
- Unusual error text can be misclassified; the failure classes of all failed
  spans are kept next to the root cause so a reviewer sees the alternatives.
- Spans arriving after finalization do not change the outcome.
