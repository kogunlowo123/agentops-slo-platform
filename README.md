# AgentOps SLO Platform

Operations platform for enterprise AI agents. Agents export OpenTelemetry
traces (GenAI semantic conventions); the platform turns every span into an
agent, tool, model, memory or workflow measurement, attributes tokens and
spend to the agent and team that caused them, evaluates service level
objectives with multi-window, multi-burn-rate alerting, classifies each
workflow run as success, recovered, failed or incomplete with a root cause,
and derives recovery actions (retry policies, circuit breaking) that pass
through policy and, when risky, a human approver before any runtime applies
them.

```
Task ─▶ Trace ─▶ Metrics ─▶ Cost ─▶ Recovery
```

## Capabilities

| Capability | Implementation |
|---|---|
| OTLP ingestion | `POST /v1/traces`, OTLP/HTTP protobuf and JSON, gzip with an inflated-size limit, partial-success responses, idempotent on (trace_id, span_id) |
| Span processing | GenAI semconv classification (`invoke_agent`, `execute_tool`, `chat`/`embeddings`, memory operations, vector stores), agent and team attribution through the span tree |
| Content safety | Prompt/completion attributes dropped at decode; error text scrubbed with agentic-core `SecretScanner` and `PiiDetector` |
| Token and cost attribution | Effective-dated price book (`PUT /v1/pricing`), cost per span at the price in force when it started, unpriced calls counted, reports by agent, team, model, workflow |
| SLOs | Availability, latency and workflow-success SLIs with selectors; error budget, time to exhaustion; Google SRE Workbook burn-rate rules (2% in 1h, 5% in 6h, 10% in 1d, 10% in 3d) |
| Alerting | Alert state in PostgreSQL, transitions audited, delivery to Alertmanager `/api/v2/alerts`; Prometheus rule export per SLO |
| Workflow classification | Deterministic root-cause search and failure classes (rate limit, timeout, guardrail, authorization, budget, loop, tool/model/memory/agent error) with recovery strategies |
| Recovery | Retry policy derived from observed conditional retry success and latency percentiles; circuit-breaker state from short and long windows; actions gated by `policies/recovery-actions.yaml`, four-eyes approval, directive feed |
| Agent interfaces | MCP tools (official `mcp` SDK, streamable HTTP) and A2A JSON-RPC `message/send` with an Agent Card |
| Identity | Agent calls authorized by agent-identity-fabric (introspect + authorize) or, without it, local JWKS verification plus `policies/agentops-access.yaml`; operators via enterprise OIDC roles |
| Audit | Every authorization decision and state change in the agentic-core signed hash chain |
| Observability | Prometheus metrics with label cardinality limits, OpenTelemetry traces, JSON logs with correlation IDs, Grafana dashboard, OTel Collector gateway configuration |

Shared controls (policy engine, audit chain, content scanners, logging,
telemetry) come from [agentic-core](https://github.com/kogunlowo123/agentic-core).

## Quick start (local)

Requires Docker. No API keys or cloud credentials are needed.

```bash
docker compose up --build
curl -s localhost:8080/.well-known/agent-card.json | jq .skills[].id
```

The compose stack runs the platform, PostgreSQL, an OIDC provider standing in
for the enterprise IdP and the agent credential issuer, and an OpenTelemetry
Collector that forwards traces to `/v1/traces`.

## Development

```bash
uv sync
make check        # ruff, black, mypy --strict, bandit, contracts, tests (coverage >= 90%)
make integration  # PostgreSQL integration tests (needs AGENTOPS_TEST_DATABASE_URL)
```

Test suites: `tests/unit`, `tests/security` (attacks mapped to the threat
model), `tests/chaos` (dependency failures), `tests/performance` (latency
budgets; `locustfile.py` for load), `tests/e2e` (full HTTP, MCP and A2A
lifecycle and a property-based OpenAPI contract test), `tests/integration`
(PostgreSQL).

## API

| Endpoint | Caller | Purpose |
|---|---|---|
| `POST /v1/traces` | Collector / agents (`agentops:traces.ingest`) | OTLP/HTTP trace export |
| `GET /v1/recovery/directives` | Agent runtimes, gateways (`agentops:directives.read`) | Active recovery directives to apply |
| `POST /a2a` | A2A peers (per-skill scope) | JSON-RPC 2.0 `message/send` |
| `POST /mcp` | MCP clients (per-tool scope) | Streamable HTTP MCP endpoint |
| `GET /.well-known/agent-card.json` | Anyone | A2A Agent Card |
| `PUT /v1/slos/{id}`, `DELETE /v1/slos/{id}` | `agentops-sre` | Manage SLOs |
| `GET /v1/slos`, `/v1/slos/{id}`, `/v1/slos/{id}/status` | Operators | SLO definitions, error budget, burn rates |
| `GET /v1/slos/{id}/prometheus-rules` | Operators | Recording and alerting rules for Prometheus |
| `POST /v1/slos:evaluate` | `agentops-sre` | Evaluate all SLOs now |
| `GET /v1/alerts`, `/v1/alerts/history` | Operators | Firing burn-rate alerts and transitions |
| `GET /v1/operations` | Operators | Calls, errors, p50/p95/p99, tokens, cost by name, agent or team |
| `GET /v1/costs` | Operators | Cost report by agent, team, model or workflow |
| `GET /v1/pricing`, `PUT /v1/pricing` | Operators / `agentops-finops` | Price book |
| `GET /v1/workflows`, `/v1/workflows/{trace_id}` | Operators | Workflow outcomes and diagnosis |
| `GET /v1/recovery/recommendations` | Operators | Retry policy and circuit state for a dependency |
| `POST /v1/recovery/actions`, `GET /v1/recovery/actions[/{id}]` | `agentops-sre` / operators | Propose and inspect recovery actions |
| `POST /v1/recovery/actions/{id}/approve`, `/reject` | `agentops-sre` (not the proposer) | Human decision |
| `GET /healthz`, `/readyz`, `/metrics` | Platform | Liveness, readiness (database), Prometheus |

Operator roles are `agentops-viewer`, `agentops-sre` and `agentops-finops`.
Full contract: [docs/api/openapi.json](docs/api/openapi.json); JSON Schemas in
[schemas/](schemas/); MCP tool definitions in
[docs/api/mcp-tools.json](docs/api/mcp-tools.json).

### MCP tools

| Tool | Scope | Returns |
|---|---|---|
| `slo_status` | `agentops:slo.read` | Error budget, burn rates, rule state |
| `firing_alerts` | `agentops:slo.read` | Firing burn-rate alerts |
| `cost_report` | `agentops:cost.read` | Tokens and spend for the last N hours |
| `workflow_diagnosis` | `agentops:workflow.read` | Outcome, failure class, root cause, recovery strategies |
| `recovery_recommendation` | `agentops:recovery.read` | Derived retry policy and circuit state |
| `propose_recovery_action` | `agentops:recovery.propose` | Action with its policy decision (active, pending approval, denied) |

### A2A skills

`health-summary`, `slo-status`, `cost-report`, `workflow-diagnosis`,
`recovery-recommendation`. A DataPart `{"skill": "...", ...}` selects a skill;
text messages are routed deterministically (a trace ID asks for a diagnosis,
cost wording asks for a cost report, anything else gets the health summary).

## Deployment

| Layer | Location |
|---|---|
| Container image | `Dockerfile` (non-root uid 10001) |
| Helm chart | `deployments/helm/agentops-slo-platform` (StatefulSet with per-replica audit volume, PDB, HPA, NetworkPolicy, ServiceMonitor) |
| Collector | `deployments/otel-collector/collector.yaml` (gateway: groups spans by trace, strips content, exports to the platform and Cloud Trace) |
| Namespace and secret sync | `deployments/kubernetes` |
| Cloud infrastructure | `deployments/terraform/{gcp,aws,azure}`; GCP is the reference (GKE, Cloud SQL PostgreSQL 16, Cloud KMS, Secret Manager, retention-locked audit bucket, Workload Identity) |
| Monitoring | `configs/prometheus/alerts.yaml`, `configs/grafana/agentops-slo-platform.json` |

See the [startup runbook](docs/runbooks/startup.md).

## Documentation

- [Architecture](docs/architecture/overview.md) and [agent specification](docs/architecture/agent-specification.md)
- [ADRs](docs/architecture/adr/)
- [Threat model and risk register](docs/security/threat-model.md)
- [Compliance control mapping](docs/compliance/control-mapping.md)
- Runbooks: [startup](docs/runbooks/startup.md), [monitoring](docs/runbooks/monitoring.md), [SLO burn rate](docs/runbooks/slo-burn-rate.md), [cost attribution](docs/runbooks/cost-attribution.md), [incident response](docs/runbooks/incident-response.md), [rollback](docs/runbooks/rollback.md), [disaster recovery](docs/runbooks/disaster-recovery.md)
- [Configuration reference](docs/operations/configuration.md)

## License

Apache-2.0
