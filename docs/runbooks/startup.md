# Runbook: first deployment and startup

## Prerequisites

- Kubernetes ≥ 1.27 (GKE regional cluster in the GCP reference) with
  Workload Identity, External Secrets Operator and a `ClusterSecretStore`
  named `platform-secrets` bound to the cloud secret manager.
- An agent-identity-fabric deployment, or an issuer whose JWKS can verify
  agent credentials.
- Enterprise IdP app registration exposing the roles `agentops-viewer`,
  `agentops-sre`, `agentops-finops`.
- Alertmanager reachable from the cluster (optional but expected).

## 1. Provision cloud resources

```bash
cd deployments/terraform/gcp        # or aws / azure
terraform init                      # configure an encrypted remote backend first
terraform apply -var-file=<environment>.tfvars
```

Record the outputs: Google service account for Workload Identity, database
URL secret name, audit bucket.

## 2. Create the audit signing key

```bash
openssl genpkey -algorithm ed25519 \
  | gcloud secrets create agentops-slo-platform-audit-key --data-file=-
```

AWS: `aws secretsmanager create-secret --secret-string file:///dev/stdin`.
Azure: `az keyvault secret set --file /dev/stdin`. The key never touches
Terraform state.

## 3. Register the platform with the identity fabric

Register `spiffe://<trust-domain>/agent/observability/agentops-slo-platform`
with its workload binding (namespace and service account of the chart). Give
the OTel Collector gateway's identity the scope `agentops:traces.ingest` and
agent identities the read scopes they need (`agentops:slo.read`,
`agentops:directives.read`, ...). Only the collector should hold the ingest
scope in production.

## 4. Namespace and secret sync

```bash
kubectl apply -k deployments/kubernetes
kubectl -n agentops get externalsecret   # all READY=True
```

## 5. Install the chart

Create `<environment>.yaml` with every value marked REQUIRED in
`deployments/helm/agentops-slo-platform/values.yaml`, plus the Workload
Identity annotation from Terraform:

```bash
helm upgrade --install agentops oci://ghcr.io/kogunlowo123/charts/agentops-slo-platform \
  --version 0.1.0 -n agentops -f <environment>.yaml --wait
```

Then deploy the collector gateway with `deployments/otel-collector/collector.yaml`
and point agent SDK exporters at it.

## 6. Verify

```bash
kubectl -n agentops port-forward svc/agentops-agentops-slo-platform 8080:80
curl -sf localhost:8080/readyz
curl -s localhost:8080/.well-known/agent-card.json | jq '.skills[].id'
curl -s localhost:8080/metrics | grep agentops_spans_ingested_total
```

Expected: `{"status":"ready"}`, five skills, and the ingestion counter rising
once agents export. With `AGENTOPS_SLO_SEED_FILE` set, `GET /v1/slos` lists
the five baseline SLOs. Set prices before relying on cost reports
([cost attribution](cost-attribution.md)).
