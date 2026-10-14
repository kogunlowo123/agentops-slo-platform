# ADR-0001: OTLP/HTTP ingestion into a span-fact store

- Status: Accepted
- Date: 2026-10-14

## Context

Agents already emit OpenTelemetry traces through their frameworks, and the
GenAI semantic conventions define how agent, tool and model operations are
described. We need SLIs, costs and workflow outcomes from that telemetry
without asking teams to instrument twice, and without becoming a general
trace store.

## Decision

- Accept OTLP/HTTP at `POST /v1/traces` in both protobuf and OTLP/JSON
  encodings, gzip or identity, decoded with `opentelemetry-proto`. OTLP/JSON
  hex IDs are converted so one protobuf code path handles both encodings.
- Keep one narrow *span fact* per span: identifiers, kind, subject name,
  attributed agent and team, service, model, timestamps in Unix nanoseconds,
  error type and scrubbed message, tokens, cost, and the authenticated
  exporter. Other attributes, prompt and completion content in particular,
  are discarded at decode time.
- Key facts by (trace_id, span_id) and insert with `ON CONFLICT DO NOTHING`
  so exporter retries are harmless and metrics count only new facts.
- Compute SLIs with indexed window queries over facts; keep facts for
  `AGENTOPS_RETENTION_DAYS` (35 by default, longer than the 30-day SLO
  window). The collector keeps exporting full traces to the trace backend
  (Cloud Trace in the GCP reference) for drill-down.

## Consequences

- Exact SLIs over arbitrary selectors and thresholds, which pre-aggregated
  metrics cannot provide.
- Storage grows with span volume; partitioning and rollups are the planned
  next steps (overview, "Cost and scalability").
- Integer nanoseconds give identical window arithmetic on PostgreSQL and
  SQLite, which keeps the test suite representative.
