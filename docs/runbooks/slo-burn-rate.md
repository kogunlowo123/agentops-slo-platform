# Runbook: AgentSloErrorBudgetBurn

Raised by the platform (Alertmanager labels `alertname`, `slo`, `rule`,
`severity`, `team`) or by the exported Prometheus rules.

- `severity=page` (`page-1h-5m`, `page-6h-30m`): the budget will be gone in
  hours to days at the current rate. Act now.
- `severity=ticket` (`ticket-1d-2h`, `ticket-3d-6h`): slow erosion. Handle in
  working hours.

## 1. Confirm and size

```bash
curl -s -H "Authorization: Bearer $OIDC" $AGENTOPS/v1/slos/$SLO/status | jq \
  '{health, error_budget_remaining, time_to_exhaustion_hours, rules: [.rules[] | select(.firing)]}'
```

Both windows of a firing rule are above the factor; the short window tells
you whether it is still burning.

## 2. Find the failing dependency

```bash
curl -s -H "Authorization: Bearer $OIDC" \
  "$AGENTOPS/v1/operations?kind=tool&group_by=name&window_minutes=30" \
  | jq '.rows | sort_by(-.error_rate) | .[:5]'
curl -s -H "Authorization: Bearer $OIDC" \
  "$AGENTOPS/v1/workflows?outcome=failed&hours=1&limit=20" \
  | jq '.items[] | {workflow, failure_class, root_cause_kind, root_cause_name}'
```

Repeat with `kind=model` or `kind=memory`. A dominant `failure_class`
(`rate_limited`, `timeout`, `tool_error` ...) and root-cause name identify
the dependency.

## 3. Get the recommendation

```bash
curl -s -H "Authorization: Bearer $OIDC" \
  "$AGENTOPS/v1/recovery/recommendations?kind=tool&name=$TOOL" | jq '{circuit, retry_policy}'
```

`circuit.state=open` means the short window is above the threshold with
enough calls. `retry_policy.max_attempts=1` with a rationale about
correlated failures means retries are adding load and should not be raised.

## 4. Propose and approve

```bash
curl -s -X POST -H "Authorization: Bearer $OIDC" -H 'Content-Type: application/json' \
  $AGENTOPS/v1/recovery/actions -d '{
    "action": "circuit.open",
    "target": {"kind": "tool", "name": "'$TOOL'"},
    "parameters": {"half_open_after_s": 120},
    "reason": "page-1h-5m on tool-call-availability; tool failing 80% in 5m",
    "ttl_minutes": 30}'
```

The response has `status=pending_approval`. A second SRE (not the proposer)
approves:

```bash
curl -s -X POST -H "Authorization: Bearer $OIDC_SECOND_SRE" -H 'Content-Type: application/json' \
  $AGENTOPS/v1/recovery/actions/$ID/approve -d '{"comment": "confirmed with tool owner"}'
```

## 5. Confirm the directive is applied

Agent runtimes and gateways poll `GET /v1/recovery/directives`. Check the
tool's call rate in `/v1/operations` drops, and that dependent workflows now
fail fast or use their fallback. The directive expires after its TTL; renew
it with a new proposal if the dependency is still down.

## 6. Recover

When the dependency is healthy, the recommendation moves to `half_open` and
then `closed`. An SRE may propose `circuit.close` (allowed directly for
SREs). Burn-rate alerts resolve once the short window drops below the factor.
Record the incident and the budget spent.
