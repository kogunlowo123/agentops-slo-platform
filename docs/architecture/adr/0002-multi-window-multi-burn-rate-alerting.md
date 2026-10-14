# ADR-0002: Multi-window, multi-burn-rate SLO alerting

- Status: Accepted
- Date: 2026-10-14

## Context

Threshold alerts on error rates either page on short blips or miss slow
budget erosion. Agent workloads are bursty, which makes this worse.

## Decision

Use the method from the Google SRE Workbook ("Alerting on SLOs"). Burn rate
is `(bad / total) / (1 - objective)`. Four rules each pair a long window
with a short window one twelfth its length; a rule fires only when both
windows meet its factor.

| Rule | Severity | Long / short | Budget spent in long window |
|---|---|---|---|
| page-1h-5m | page | 1h / 5m | 2% |
| page-6h-30m | page | 6h / 30m | 5% |
| ticket-1d-2h | ticket | 1d / 2h | 10% |
| ticket-3d-6h | ticket | 3d / 6h | 10% |

The factor is `budget_fraction × SLO window / long window`: 14.4, 6, 3 and 1
for a 30-day SLO, and still correct for other window lengths. Windows with
fewer than `min_events` events count as no data and cannot fire. The same
rule set drives the platform's evaluator and the Prometheus rules it
exports (`GET /v1/slos/{id}/prometheus-rules`).

## Consequences

- Sharp outages page quickly, transient spikes do not, and the short window
  resets alerts soon after recovery.
- Low-traffic SLOs may never alert; owners choose `min_events` and
  objectives knowingly (risk register R4).
- Latency SLOs exported to Prometheus must use a histogram bucket bound as
  threshold; native evaluation has no such restriction.
