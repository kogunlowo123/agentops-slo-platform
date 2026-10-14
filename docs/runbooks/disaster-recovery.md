# Runbook: disaster recovery

| Component | Protection | RPO | RTO target |
|---|---|---|---|
| PostgreSQL | Regional HA, automated backups (14 days), point-in-time recovery | Minutes | 1 hour |
| Audit chains | Per-replica volume, shipped to retention-locked bucket | Shipping interval | n/a (append-only evidence) |
| Configuration | Git (chart values, policies, SLO seed file) | 0 | Redeploy |
| Audit key | Secret manager with versioning | 0 | Minutes |

## Region loss

1. Restore the database from the latest cross-region backup or PITR into the
   recovery region (`gcloud sql backups restore` / `instances clone`).
2. Apply Terraform for the recovery region and install the chart pointing at
   the restored database.
3. Repoint the collector gateway exporters. Spans exported during the outage
   remain in collector queues only up to their capacity; SLO windows will
   show a gap, which the incident record should note.
4. Re-run `POST /v1/slos:evaluate` and check that firing alerts match
   reality before re-enabling paging.

## Database corruption or bad data

Restore to a point in time before the event into a new instance, verify
`slos`, `prices` and `recovery_actions`, then switch the database URL secret.
Span facts lost in between affect only SLI history for that period.

## Audit chain verification

After any restore, verify each replica's chain with agentic-core
`verify_chain` against the audit public key. A new replica starts a new
chain; chains are never rewritten.
