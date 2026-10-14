# Runbook: monitoring the platform

Load `configs/prometheus/alerts.yaml` and the dashboard
`configs/grafana/agentops-slo-platform.json`.

## Signals

| Signal | Metric | Healthy |
|---|---|---|
| Ingestion errors | `agentops_http_request_duration_seconds_count{route="/v1/traces",status=~"5.."}` | ~0 |
| Ingestion latency | `histogram_quantile(0.99, ... route="/v1/traces")` | < 1 s |
| Rejected spans | `agentops_spans_rejected_total` by `reason` | Low and explained |
| Duplicates | `agentops_spans_duplicate_total` | Spikes only during exporter retries |
| Redactions | `agentops_redactions_total` | Any rise means an agent echoes sensitive input into errors; tell the owning team |
| Unpriced calls | `agentops_unpriced_model_calls_total` by `model` | 0 |
| Label overflow | `agentops_label_overflow` by `label` | Absent |
| Background jobs | `agentops_background_failures_total` by `loop` | 0 |
| Alert delivery | `agentops_alert_notifications_failed_total` | 0 |
| Authorization | `agentops_authorization_decisions_total` by `decision` | Denials and `unauthenticated` low |

## Latency

If `/v1/traces` p99 rises: check database CPU and I/O first (inserts and
index maintenance dominate), then batch sizes in the collector (`batch`
processor). Large batches amortize auth and transactions; very large
batches raise per-request latency.

## Freshness

SLO status reflects spans whose end time is at or before evaluation time.
If `agentops_slo_burn_rate` stops changing while traffic flows, check
`agentops_background_failures_total{loop="evaluate_slos"}` and the logs for
`background_job_failed`.

## Workflows stuck unfinalized

Runs are classified after their root span plus the quiet period. Many runs
ending `incomplete` usually means the collector drops root spans (sampling
or a broken exporter in the entry service).
