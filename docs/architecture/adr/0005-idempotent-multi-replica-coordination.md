# ADR-0005: Idempotent writes instead of leader election

- Status: Accepted
- Date: 2026-10-14

## Context

Several replicas ingest concurrently and each runs the background jobs
(workflow finalization, SLO evaluation, retention purge). Without
coordination, workflows would be counted twice and alerts raised twice.

## Decision

Make every contended write conditional in the database, and let only the
replica whose write changed a row emit the side effects:

- span facts: primary key plus `ON CONFLICT DO NOTHING`;
- workflow finalization: `UPDATE ... WHERE finalized = false`;
- alert transitions: `UPDATE slo_alert_state ... WHERE firing = <previous>`;
- recovery decisions: compare-and-set on status.

No leases or leader election are used.

## Consequences

- Any replica can stop at any time without failover logic.
- Evaluation queries run on every replica. They are read-only and cheap
  relative to ingestion, and the design stays simple.
- Every replica re-sends firing alerts to Alertmanager, which de-duplicates
  identical label sets.
