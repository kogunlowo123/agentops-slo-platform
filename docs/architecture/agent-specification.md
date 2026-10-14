# Agent specification

The platform is itself an agent-facing service: other agents send it
telemetry, query it over MCP and A2A, and act on its recovery directives.
This document states its mandate in the same terms the identity fabric
requires of every registered agent.

## Role and mission

- **Purpose:** measure the reliability and cost of the enterprise's AI
  agents against declared objectives, explain failures, and propose safe
  recovery.
- **Business objective:** let teams run agents in production with error
  budgets, accountable spend and controlled remediation, the way they run
  services today.
- **Protected resources:** stored span facts and workflow diagnoses, the
  price book, SLO definitions and alert state, recovery actions and
  directives, the audit chain.
- **Operating boundaries:** observes and recommends; never calls models,
  never changes another system directly. Changes to runtime behaviour reach
  agents only as directives they pull, and only after policy (and, where
  required, a human) has allowed them.
- **Delegated responsibility:** which recovery actions run without a human
  is delegated to `policies/recovery-actions.yaml`, owned by SRE and AI
  governance; access by agents is delegated to the identity fabric policy.

## Delegated scope

| Allowed | Disallowed | Risk | Escalation |
|---|---|---|---|
| Ingest spans from authenticated exporters | Store prompt or completion content | LOW | Rejection metrics and alerts |
| Compute SLIs, budgets, burn rates; raise alerts | Suppress or edit alert history | MODERATE | Alertmanager page/ticket to the SLO owner |
| Classify workflow outcomes | Change outcomes after finalization | LOW | n/a |
| Activate retry policies within guardrails (≤ 3 attempts; agents and SREs) | Retry policies above 5 attempts | LOW | > 3 attempts requires approval |
| Close circuits (SRE) | Close circuits on agent proposal alone | MODERATE | SRE approval |
| Record circuit opening, budget raises | Activate them without a second person | HIGH | `require_approval`, four eyes |
| Record agent pause proposals from operators | Accept agent pause proposals from agents | CRITICAL | `require_approval`; agents denied |

## Objectives

| Type | Definition | Target |
|---|---|---|
| SLO availability | Non-5xx share of `/v1/traces` responses | 99.9% over 30 days |
| SLO freshness | Span visible to SLO evaluation after export | ≤ 2 evaluation intervals (default 2 min) |
| SLO latency | p99 `/v1/traces` for batches ≤ 1,000 spans | < 1 s |
| SLI | `agentops_http_request_duration_seconds`, `agentops_spans_rejected_total`, `agentops_background_failures_total` | Prometheus |
| KPI | Share of model calls with a configured price | 100% (`agentops_unpriced_model_calls_total` = 0) |
| KPI | Share of agent spans attributed to a team | Tracked via `/v1/costs?group_by=team` row `unattributed` |
| Completion | An ingestion request is complete when its new facts are committed; a decision is complete when its audit event is written | Enforced: audit failure fails the request |

## Permissions

| Mechanism | Use |
|---|---|
| RBAC (operators) | Enterprise OIDC roles: `agentops-viewer` (read), `agentops-sre` (SLOs, recovery proposals and approvals), `agentops-finops` (price book) |
| ABAC (agents) | Scopes on fabric credentials: `agentops:traces.ingest`, `agentops:slo.read`, `agentops:cost.read`, `agentops:workflow.read`, `agentops:recovery.read`, `agentops:recovery.propose`, `agentops:directives.read`; resource classification (cost data is CONFIDENTIAL); delegation and risk attributes |
| Agent identity | SPIFFE IDs issued by agent-identity-fabric |
| OAuth 2.0 | Fabric `POST /oauth2/introspect` (RFC 7662) and `POST /v1/authorize` for every uncached agent call; `POST /oauth2/token` jwt-bearer (RFC 7523) for the platform's own credential toward Alertmanager |
| OIDC | Operator tokens verified against the IdP JWKS (issuer, audience, roles claim) |
| Workload identity | Projected service-account token as the jwt-bearer assertion; GKE Workload Identity (IRSA, Azure Workload Identity) for audit storage |

## Inputs and outputs

| Interface | Input schema | Output schema |
|---|---|---|
| OTLP | `opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest` | `ExportTraceServiceResponse` |
| SLOs | `schemas/slo-definition.schema.json` | `schemas/slo-status.response.schema.json` |
| Pricing | `schemas/price-entry.schema.json` | same |
| Costs | query parameters | `schemas/cost-report.response.schema.json` |
| Operations | query parameters | `schemas/operation-report.response.schema.json` |
| Workflows | trace ID | `schemas/workflow.response.schema.json` |
| Recovery | `schemas/propose-action.request.schema.json` | `schemas/recovery-action.response.schema.json`, `schemas/recovery-recommendation.response.schema.json` |
| Stored fact | n/a | `schemas/span-fact.schema.json` |
| HTTP API | `docs/api/openapi.json` | same |
| MCP | `docs/api/mcp-tools.json` (input and output schema per tool) | same |
| A2A | Agent Card at `/.well-known/agent-card.json` | Task with DataPart and TextPart |

## Validation

- OTLP: content type and encoding allow-listed; body ≤ 8 MiB, inflated ≤
  32 MiB, ≤ 10,000 spans; trace and span IDs must have the correct length
  and be non-zero; end ≥ start; end not more than 10 minutes in the future
  and not older than retention.
- Strings from telemetry are reduced to printable ASCII and 120 characters
  before they become labels or keys; error messages are scrubbed and capped
  at 512 characters.
- Request bodies reject unknown fields; SLO IDs are slugs; latency SLOs
  require a threshold; objectives lie in (0.5, 1).
- Recovery parameters are validated per action type and target kind.

## Provenance

Every span fact records `ingested_by`, the authenticated subject that
exported it. Every authorization decision and every recovery decision
records the policy version and digest that produced it. Workflow diagnoses
name the root-cause span ID so an investigator can open the original trace.

## Audit metadata

Audit events (agentic-core `AuditLog`, hash-chained and Ed25519-signed)
carry actor, action, resource, outcome, correlation ID and details. Recorded
actions: agent authorization decisions (including unauthenticated
attempts), MCP tool and A2A skill invocations, `slo.create|update|delete`,
`slo.alert.firing|resolved`, `pricing.set`, `recovery.propose.<action>`,
`recovery.approve`, `recovery.reject`. Credentials and telemetry content are
never written to the audit chain.
