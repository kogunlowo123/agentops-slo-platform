# Runbook: cost attribution

## Setting prices

Prices are configuration. Use the rates in your contract, per model name as
it appears in `gen_ai.response.model` (or `gen_ai.request.model`), in USD per
million tokens. Each entry takes effect at `effective_from_ns`; spans are
priced with the rate in force when they started, so a new rate never changes
past costs.

| Field | Value |
|---|---|
| `model` | Model name exactly as it appears in telemetry |
| `provider` | Optional, e.g. the `gen_ai.provider.name` value |
| `input_per_mtok`, `output_per_mtok` | Contracted USD rate per million tokens |
| `cached_input_per_mtok` | Optional cache-read rate; omitted means the input rate applies |
| `effective_from_ns` | Unix epoch nanoseconds when the rate starts |

The finance team supplies the values; the shell variables below hold them.

```bash
jq -n --arg model "$MODEL" --argjson input "$INPUT_RATE" --argjson output "$OUTPUT_RATE" \
      --argjson from "$(date -u -d "$EFFECTIVE_FROM" +%s)000000000" \
      '{model: $model, input_per_mtok: $input, output_per_mtok: $output, effective_from_ns: $from}' \
  | curl -s -X PUT -H "Authorization: Bearer $OIDC_FINOPS" \
      -H 'Content-Type: application/json' --data @- "$AGENTOPS/v1/pricing"
curl -s -H "Authorization: Bearer $OIDC" "$AGENTOPS/v1/pricing" | jq
```

Requires the `agentops-finops` role; every change is audited
(`pricing.set`). Replicas pick up changes within 30 seconds.

## Unpriced calls

`agentops_unpriced_model_calls_total{model=...}` counts model calls with
tokens but no applicable price; cost reports show them as `unpriced_calls`.
Add the missing model (check exact spelling, including version suffixes)
with an `effective_from_ns` at or before the first unpriced call. Costs
are computed at ingestion, so calls ingested before the price existed stay
unpriced in stored facts and Prometheus; report them separately for that
period.

## Attribution quality

`/v1/costs?group_by=team` returns a row `unattributed` for spend with no
`agentops.team` on the span, its ancestors or the resource. Ask owning teams
to set the resource attribute `agentops.team` in their SDK or have the
collector add it. Agent attribution follows `gen_ai.agent.name` on the
nearest ancestor `invoke_agent` span.

## Reconciling with invoices

1. Pull `/v1/costs?group_by=model&start_ns=...&end_ns=...` for the invoice
   period (UTC boundaries).
2. Compare tokens per model with the provider's usage export, not money:
   token counts are measured, money depends on the price book.
3. Differences usually come from calls made outside instrumented agents,
   spans dropped by sampling, or unpriced models. Investigate before
   adjusting prices.
