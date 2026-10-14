# Threat model

## Assets

| Asset | Impact if compromised |
|---|---|
| Span facts and workflow diagnoses | Wrong SLOs, hidden outages, disclosure of operational detail |
| Telemetry content in transit (prompts, tool arguments, error text) | Disclosure of personal data, PHI or secrets |
| Price book and cost reports | Misallocated spend, disclosure of contracted rates |
| SLO definitions and alert state | Silenced paging, alert fatigue |
| Recovery actions and directives | Traffic cut off from healthy dependencies or restored to failing ones |
| Audit chains and audit key | Loss of accountability |
| Operator and agent access | All of the above |

## Trust boundaries

1. Collector and agents → `/v1/traces`, `/mcp`, `/a2a`, `/v1/recovery/directives` (untrusted network callers with agent credentials).
2. Operators → `/v1/*` management endpoints (enterprise OIDC).
3. Platform → identity fabric, IdP JWKS, Alertmanager.
4. Platform → PostgreSQL, secret manager, audit storage.
5. Telemetry content itself: attacker-influenced strings (model output, tool errors, user input echoed into exceptions) inside otherwise authenticated exports.

## STRIDE

| ID | Threat | Category | Control | Test |
|---|---|---|---|---|
| S1 | Unauthenticated span export | Spoofing | Bearer required before the body is read; fabric or local JWKS verification | `tests/security/test_ingestion_attacks.py::test_s1_unauthenticated_export_rejected` |
| S2 | Valid agent without ingest rights writes telemetry | Elevation | Per-action scope `agentops:traces.ingest`; default-deny policy | `test_s2_token_without_ingest_scope_forbidden` |
| S3 | Forged credential | Spoofing | Signature verification against JWKS, issuer and audience checks | `test_s3_forged_token_rejected` |
| S4 | `alg: none` or algorithm confusion | Spoofing | Algorithm allow list, `kid` required | `test_s4_alg_none_rejected` |
| S5 | Gzip bomb | DoS | Streaming inflate with a hard inflated-size limit | `test_s5_gzip_bomb_rejected` |
| S6 | Oversized body or span count | DoS | Content-Length and streamed size limit, span-count limit | `test_s6_oversized_body_rejected` |
| S7 | Secrets or personal data in error text stored | Information disclosure | agentic-core `SecretScanner` and `PiiDetector` redaction, 512-character cap | `test_s7_secrets_and_pii_scrubbed_from_error_text` |
| S8 | Prompt or completion content persisted | Information disclosure | Content attributes dropped at decode; collector deletes them first | `test_s8_prompt_content_attributes_never_stored` |
| S9 | Unbounded label values exhaust Prometheus | DoS | Per-label cardinality limiter folds excess into `other` | `test_s9_label_cardinality_flood_is_capped` |
| S10 | Spans dated in the future or far past distort SLO windows | Tampering | Rejected beyond 10 minutes ahead or past retention | `test_s10_future_timestamps_cannot_poison_windows` |
| S11 | Replayed export inflates counts | Tampering | (trace_id, span_id) primary key; metrics only for new facts | `test_s11_duplicate_export_not_double_counted` |
| S12 | Exporter claims another agent's name to shift cost or blame | Spoofing / repudiation | `ingested_by` records the authenticated caller on every fact; collector stamps `agentops.team` from namespace labels | `test_s12_spoofed_agent_names_keep_ingest_provenance` |
| S13 | Operator token or foreign-issuer token used as an agent credential | Spoofing | Separate issuers and key sets for operators and agents; algorithm allow list | `test_s13_key_confusion_between_issuers` |
| G1 | Operator without role changes SLOs or prices | Elevation | Role checks per endpoint | `tests/security/test_governance_attacks.py::test_g1_operator_without_role_forbidden` |
| G2 | One person proposes and approves a risky action | Elevation / repudiation | Approver must differ from proposer; compare-and-set | `test_g2_proposer_cannot_self_approve` |
| G3 | Agent pauses another agent | Elevation | Policy rule `deny-agent-pause-by-agents` | `test_g3_agent_cannot_pause_agents` |
| G4 | High-risk action applied without a human | Elevation | `require_approval` for risk ≥ HIGH; directives only from active actions | `test_g4_high_risk_action_never_auto_applied` |
| G5 | MCP tool called without credentials | Spoofing | Per-tool authorization, tool error on failure | `test_g5_mcp_tool_without_credentials_denied` |
| G6 | A2A call without credentials | Spoofing | HTTP 401 before skill dispatch | `test_g6_a2a_without_credentials_unauthorized` |
| G7 | Audit tampering, or anonymous probes flooding the evidence chain | Repudiation / DoS | Hash-chained, Ed25519-signed events, retention-locked storage; requests with no credential are counted (`decision="missing_credentials"`) but not written to the chain, while presented-but-invalid credentials are audited | `test_g7_audit_chain_verifies_after_operations` |
| G8 | Delegate proposes recovery on another agent's behalf | Elevation | Policy rule `deny-delegated-recovery-proposals` | `test_g8_delegated_agent_cannot_propose_recovery` |
| C1 | Database outage loses telemetry silently | DoS | `/readyz` 503; ingestion 503 with `Retry-After` so exporters retry | `tests/chaos/test_dependency_failures.py::test_database_outage_flips_readiness_and_ingestion_returns_503` |
| C2 | Fabric outage opens access | Elevation | Transport errors and 5xx fail closed (503) | `test_fabric_outage_fails_closed` |
| C3 | Alertmanager outage stalls evaluation | DoS | Delivery failure counted and logged; state already persisted; next cycle re-sends | `test_alertmanager_outage_does_not_block_evaluation` |
| C4 | Recovery action without evidence | Repudiation | Audit write failure fails the request | `test_audit_sink_failure_blocks_recovery_action` |
| C5 | Background job crash stops evaluation | DoS | Each iteration isolated, failures counted | `test_background_loop_survives_failures` |

## MITRE ATT&CK (Enterprise) techniques addressed

| Technique | Mitigation |
|---|---|
| T1078 Valid Accounts | Short-lived fabric credentials, scope per action, introspection |
| T1528 Steal Application Access Token | Decision cache bounded by token expiry; tokens never logged or audited |
| T1499 Endpoint Denial of Service | Body, inflate and span limits; HPA; ingress rate limiting |
| T1565.001 Stored Data Manipulation | Idempotent keys, timestamp bounds, `ingested_by` provenance |
| T1562.006 Impair Defenses: Indicator Blocking | Alert state transitions audited; SLO deletion audited and role-restricted |
| T1552 Unsecured Credentials | Secret scanner on stored error text |

## MITRE ATLAS

| Technique | Relevance | Mitigation |
|---|---|---|
| AML.T0051 LLM Prompt Injection | Injected text can reach telemetry through model output and tool errors | No model in the control loop (ADR-0007); content attributes dropped; classification uses fixed rules |
| AML.T0024 Exfiltration via ML Inference API | Prompts and outputs carried in spans could leak through an observability store | Content never stored; scrubbing; cost data CONFIDENTIAL |
| AML.T0034 Cost Harvesting | Abuse that drives model spend | Per-agent and per-team cost attribution, unpriced-call alerts, budget actions behind approval |

## OWASP Top 10 for LLM Applications (2025)

| Risk | Relevance | Control |
|---|---|---|
| LLM10 Unbounded Consumption | Primary: the platform is where runaway spend becomes visible | Cost attribution, loop detection, burn-rate alerts, `budget.raise` gated |
| LLM02 Sensitive Information Disclosure | Telemetry carries user data | Content dropped, error text scrubbed |
| LLM06 Excessive Agency | Agents acting on recovery advice | Directives only after policy and approval; agents cannot pause agents |
| LLM01 Prompt Injection | Indirect, through telemetry | No model consumes telemetry |

## OWASP API Security Top 10 (2023)

| Risk | Control |
|---|---|
| API1 Broken Object Level Authorization | Agent calls authorized per resource ID (SLO, trace, dependency) |
| API2 Broken Authentication | JWKS verification, algorithm allow list, issuer and audience checks |
| API3 Broken Object Property Level Authorization | `extra="forbid"` on inputs; facts exclude content |
| API4 Unrestricted Resource Consumption | Size, span and page limits; report ranges ≤ 93 days |
| API5 Broken Function Level Authorization | Viewer, SRE and FinOps roles separate; four-eyes approvals |
| API6 Unrestricted Access to Sensitive Business Flows | Duplicate live actions rejected (409); TTL on every action |
| API8 Security Misconfiguration | Startup refuses missing authorization config and ephemeral keys in production; Helm `required` values |
| API10 Unsafe Consumption of APIs | Fabric and Alertmanager responses validated; timeouts; fail closed |

## Risk register

| ID | Risk | Likelihood | Impact | Owner | Treatment | Residual |
|---|---|---|---|---|---|---|
| R1 | Cost spoofing: an exporter forges `gen_ai.agent.name` or `agentops.team` to shift spend | Medium | Medium | FinOps | `ingested_by` recorded on every fact; in production only the collector identity holds `agentops:traces.ingest`; the collector can stamp `agentops.team` from trusted Kubernetes resource attributes | Low |
| R2 | Telemetry content leakage through attributes not on the drop list | Medium | High | Security | Facts keep only named fields; collector deletes content attributes; review of new semconv attributes | Low |
| R3 | Cardinality DoS on metrics | Medium | Medium | Platform | Label limiter; `agentops_label_overflow` gauge watched on the dashboard | Low |
| R4 | Alert fatigue or silence on low-traffic SLOs | Medium | Medium | SLO owners | `min_events`, ticket vs page severities, quarterly SLO review | Medium |
| R5 | Clock skew between agents and platform | Medium | Low | Platform | 10-minute future bound; windows use span end time; NTP on nodes | Low |
| R6 | Revocation delay from decision cache | Low | Medium | Security | TTL ≤ 15 s and never beyond token expiry | Low |
| R7 | Misclassified failures lead to the wrong recovery | Medium | Medium | SRE | Recommendations only; humans approve risky actions; observed classes shown | Low |
| R8 | PostgreSQL outage | Medium | High | Platform | Regional HA, PITR, readiness probe, exporter retry | Low |
