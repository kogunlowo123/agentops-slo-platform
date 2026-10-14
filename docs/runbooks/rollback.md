# Runbook: rollback

The schema in 0.1.0 is created idempotently and only extended in later
versions, so the previous image can run against the current database.

```bash
helm -n agentops history agentops
helm -n agentops rollback agentops <revision> --wait
kubectl -n agentops rollout status statefulset/agentops-agentops-slo-platform
curl -sf localhost:8080/readyz
```

Policy-only changes (`policies/*.yaml` baked into the image) roll back with
the image. SLO definitions and prices live in the database and are not
affected by a rollback; revert them through the API, which audits the change.

Verify after rollback: ingestion counters rising, no
`agentops_background_failures_total` increase, firing alerts unchanged
unless the rolled-back release caused them.
