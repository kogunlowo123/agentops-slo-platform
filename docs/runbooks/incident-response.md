# Runbook: incident response

## Ingestion returning 5xx {#ingestion-errors}

1. `kubectl -n agentops get pods`; check `/readyz` on each replica.
2. 503 with `database unavailable`: follow the database section below.
   Exporters retry on 503, so telemetry is buffered in the collector
   (`sending_queue`) for the duration of the queue.
3. 503 from authorization: the identity fabric is unreachable. Access fails
   closed by design; restore the fabric rather than bypassing it.

## Database unavailable {#database}

1. Check Cloud SQL (or RDS / Flexible Server) status and failover events.
2. After recovery, confirm `agentops_spans_ingested_total` resumes and the
   collector queue drains.
3. SLO windows covering the outage show fewer events, not false errors.

## Suspected telemetry abuse {#abuse}

Indicators: spikes in `agentops_spans_rejected_total`,
`agentops_label_overflow`, cost suddenly attributed to an unexpected agent.

1. Find the exporter: query `span_facts.ingested_by` for the affected spans.
2. Suspend the identity in the fabric; cached decisions expire within
   `AGENTOPS_DECISION_CACHE_TTL_S`.
3. Rebuild affected cost reports after removing the forged facts; record the
   deletion in the incident.

## Sensitive data found in stored error text {#leak}

1. Identify the pattern the scrubber missed and the producing agent.
2. Delete affected rows (`UPDATE span_facts SET error_message = NULL WHERE ...`).
3. Add a detector to agentic-core (or the collector) and a regression test in
   `tests/security`.
4. Follow the organization's privacy incident process.

## Unexpected recovery directive {#directive}

1. `GET /v1/recovery/actions?status=active` and inspect `proposed_by`,
   `decided_by`, `matched_rules`, `policy_digest`.
2. To stop it early, propose the inverse action (`circuit.close`) or wait for
   TTL expiry; verify in the audit chain who approved it.
3. If the policy allowed something it should not have, fix
   `policies/recovery-actions.yaml` through review and redeploy.
