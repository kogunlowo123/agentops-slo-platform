# Configuration reference

All settings are `AGENTOPS_*` environment variables (see
`src/agentops_slo_platform/config.py`). The service refuses to start with
incomplete or inconsistent configuration.

| Variable | Required | Default | Description |
|---|---|---|---|
| `AGENTOPS_ENVIRONMENT` | no | `production` | `production` forbids ephemeral audit keys |
| `AGENTOPS_PUBLIC_URL` | yes | | External URL; used in the A2A Agent Card and alert links |
| `AGENTOPS_DATABASE_URL` | yes | | SQLAlchemy async URL (`postgresql+asyncpg://...`; SQLite for tests) |
| `AGENTOPS_AUDIT_LOG_PATH` | no | `/var/lib/agentops/audit/audit.jsonl` | Audit chain file |
| `AGENTOPS_AUDIT_KEY_FILE` | yes* | | Ed25519 PEM audit signing key |
| `AGENTOPS_DEV_EPHEMERAL_KEYS` | no | `false` | In-memory audit key, non-production only (replaces *) |
| `AGENTOPS_OPERATOR_ISSUER` | yes | | Enterprise OIDC issuer for operators |
| `AGENTOPS_OPERATOR_AUDIENCE` | yes | | Audience of operator tokens |
| `AGENTOPS_OPERATOR_JWKS_URL` | yes | | IdP JWKS URL |
| `AGENTOPS_OPERATOR_ROLES_CLAIM` | no | `roles` | Claim carrying `agentops-*` roles |
| `AGENTOPS_FABRIC_URL` | yes** | | agent-identity-fabric base URL |
| `AGENTOPS_FABRIC_AGENT_ID` | no | | This platform's SPIFFE ID; set together with the next variable |
| `AGENTOPS_WORKLOAD_TOKEN_FILE` | no | | Projected token used as jwt-bearer assertion (Alertmanager credential) |
| `AGENTOPS_AGENT_ISSUER` | yes** | | Agent token issuer when no fabric is configured |
| `AGENTOPS_AGENT_AUDIENCE` | yes** | | Agent token audience when no fabric is configured |
| `AGENTOPS_AGENT_JWKS_URL` | yes** | | Agent token JWKS when no fabric is configured |
| `AGENTOPS_ACCESS_POLICY_FILE` | no | `policies/agentops-access.yaml` | Local agent access policy |
| `AGENTOPS_DECISION_CACHE_TTL_S` | no | `15` | 0–300; authorization decision cache, never beyond token expiry |
| `AGENTOPS_RECOVERY_POLICY_FILE` | no | `policies/recovery-actions.yaml` | Recovery action policy |
| `AGENTOPS_SLO_SEED_FILE` | no | | YAML of SLOs created at start-up if missing (`configs/slos.yaml`) |
| `AGENTOPS_ALERTMANAGER_URL` | no | | Alertmanager base URL; alerts posted to `/api/v2/alerts` |
| `AGENTOPS_MAX_BODY_BYTES` | no | `8388608` | Compressed OTLP body limit |
| `AGENTOPS_MAX_INFLATED_BYTES` | no | `33554432` | Decompressed OTLP body limit |
| `AGENTOPS_MAX_SPANS_PER_REQUEST` | no | `10000` | Spans per export request |
| `AGENTOPS_MAX_FUTURE_SKEW_S` | no | `600` | Spans ending later than now + skew are rejected |
| `AGENTOPS_METRIC_LABEL_LIMIT` | no | `500` | Distinct values per telemetry-derived metric label |
| `AGENTOPS_RETENTION_DAYS` | no | `35` | Span fact and workflow retention; older spans are rejected at ingestion |
| `AGENTOPS_WORKFLOW_QUIET_S` | no | `30` | Quiet period after the root span before a run is classified |
| `AGENTOPS_WORKFLOW_MAX_WAIT_S` | no | `900` | Runs without a root are classified `incomplete` after this; must exceed the quiet period |
| `AGENTOPS_LOOP_THRESHOLD` | no | `25` | Tool calls under one parent above which a run is `loop_detected` |
| `AGENTOPS_EVALUATION_INTERVAL_S` | no | `60` | SLO evaluation period |
| `AGENTOPS_FINALIZE_INTERVAL_S` | no | `10` | Workflow finalization period |
| `AGENTOPS_PURGE_INTERVAL_S` | no | `3600` | Retention purge period |
| `AGENTOPS_MCP_ALLOWED_HOSTS` | no | `[]` | JSON list of Host headers accepted by `/mcp`; empty disables DNS-rebinding checks |
| `AGENTOPS_LOG_LEVEL` | no | `INFO` | |
| `AGENTOPS_OTLP_ENDPOINT` | no | | OTLP gRPC endpoint for the platform's own traces |
| `AGENTOPS_HOST` / `AGENTOPS_PORT` | no | `127.0.0.1` / `8080` | Bind address; the container image sets host `0.0.0.0` |
| `AGENTOPS_FORWARDED_ALLOW_IPS` | no | `127.0.0.1` | Proxies trusted for `X-Forwarded-*` |

\* Unless `AGENTOPS_DEV_EPHEMERAL_KEYS=true` outside production.
\** Either `AGENTOPS_FABRIC_URL`, or all three `AGENTOPS_AGENT_*` variables.
