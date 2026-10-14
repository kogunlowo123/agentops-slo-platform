# Compliance control mapping

The evidence column names the artifact an auditor can inspect. Mappings show
where the platform contributes to a control; the control itself belongs to
the deploying organization.

| Control objective | SOC 2 (TSC 2017) | ISO/IEC 27001:2022 | NIST SP 800-53 r5 | NIST AI RMF 1.0 | HIPAA Security Rule | Evidence |
|---|---|---|---|---|---|---|
| Measure AI system performance against objectives | CC4.1, A1.1 | 8.16 | CA-7, SI-4 | MEASURE 2.6, MEASURE 4.2 | 164.308(a)(8) | SLO definitions, `/v1/slos/{id}/status`, `agentops_slo_*` metrics |
| Detect and escalate degradation | CC7.2, CC7.3 | 5.24, 8.16 | IR-4, SI-4(5) | MANAGE 4.1 | 164.308(a)(6) | Burn-rate rules, `slo_alert_events`, Alertmanager routes |
| Track and attribute AI resource consumption | CC3.2 | 5.9 | PM-3 | GOVERN 1.6, MANAGE 2.1 | n/a | Price book history, cost reports by agent and team |
| Explain failures of AI workflows | CC7.4 | 5.27 | IR-5, AU-6 | MEASURE 3.1, MANAGE 4.3 | 164.308(a)(6)(ii) | Workflow diagnoses with root-cause span IDs |
| Human oversight of corrective actions | CC8.1 | 8.32 | CM-3, AC-5 | GOVERN 3.2, MANAGE 2.4 | 164.308(a)(3) | `recovery_actions` with proposer, approver, policy digest |
| Least privilege for agents and operators | CC6.1, CC6.3 | 5.15, 8.2 | AC-3, AC-6 | GOVERN 1.4 | 164.312(a)(1) | Scopes per action, operator roles, `policies/*.yaml` |
| Authentication of callers | CC6.1 | 5.17, 8.5 | IA-2, IA-9 | n/a | 164.312(d) | Fabric introspection and authorization, JWKS verification |
| Minimize sensitive data in telemetry | CC6.7, P4.1 | 5.34, 8.11 | SI-12, PT-2 | MAP 4.1, MEASURE 2.10 | 164.502(b) minimum necessary, 164.312(e)(1) | Content attributes dropped, scrubbed error text, `agentops_redactions_total` |
| Audit trail integrity | CC7.2 | 8.15 | AU-9, AU-10 | MEASURE 2.8 | 164.312(b) | Signed chains in retention-locked storage, `verify_chain` |
| Change control of policies and SLOs | CC8.1 | 8.32 | CM-3, CM-5 | GOVERN 1.4 | 164.308(a)(8) | Git history, CODEOWNERS, audited SLO changes |
| Availability of the monitoring function | A1.2 | 8.14 | CP-10 | n/a | 164.308(a)(7) | HA database, PDB, DR runbook |
| Data retention | CC6.5 | 5.33 | SI-12 | n/a | 164.316(b)(2) | `AGENTOPS_RETENTION_DAYS`, purge job, bucket retention |

## HIPAA

Telemetry must not carry PHI. The platform enforces this as defence in depth:
prompt, completion, tool-argument and tool-result attributes are discarded at
decode, error messages are scrubbed for identifiers (SSN, MRN, date of birth,
email, phone, card numbers) and secrets before storage, and the collector
deletes content attributes before export. Organizations processing PHI still
need a BAA with any trace backend that receives raw spans.

## GxP and 21 CFR Part 11

The platform is not a GxP record system. Where agents participate in
regulated processes, its audit chain gives attributable, time-stamped,
tamper-evident records of who changed SLOs, prices and recovery state and who
approved risky actions, which supports 11.10(e) audit trail expectations for
the monitoring layer. Validation of a deployment can use the CI run of the
deployed tag as operational qualification evidence for the requirements in
`docs/architecture/agent-specification.md`.
