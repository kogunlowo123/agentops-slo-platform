# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [0.1.0] - 2026-10-14

### Added

- OTLP/HTTP trace ingestion (protobuf and JSON, gzip) with partial-success responses,
  idempotent storage keyed by trace and span ID, and size, span-count and timestamp limits.
- Span classification by OpenTelemetry GenAI semantic conventions into agent, tool, model,
  memory and workflow operations; agent and team attribution through the span tree.
- Removal of prompt and completion attributes at decode time and scrubbing of secrets and
  personal data from stored error text.
- Effective-dated price book, per-span cost attribution and cost reports by agent, team,
  model and workflow.
- SLO definitions (availability, latency, workflow success), error budgets and
  multi-window, multi-burn-rate alerting with Alertmanager delivery and Prometheus rule export.
- Workflow outcome classification with root-cause failure classes and recovery strategies.
- Retry-policy and circuit-breaker recommendations; policy-gated recovery actions with
  four-eyes approval and a directive feed for agent runtimes.
- MCP tools (official `mcp` SDK, streamable HTTP) and A2A Agent Card with JSON-RPC
  `message/send`.
- Agent authorization through agent-identity-fabric or local JWKS plus policy; operator
  authentication through enterprise OIDC roles; signed audit chain.
- Helm chart, Kubernetes manifests, OpenTelemetry Collector gateway configuration and
  Terraform for GCP, AWS and Azure.
- CI: lint, types, SAST, contract drift, tests (unit, security, chaos, performance, e2e,
  PostgreSQL integration), Helm/kubeconform, Terraform validate, Trivy, Syft, Grype,
  Semgrep, container smoke test.
