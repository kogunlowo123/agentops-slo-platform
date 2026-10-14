output "gcp_service_account" {
  description = "Annotate the service account with iam.gke.io/gcp-service-account = this value"
  value       = google_service_account.agentops.email
}

output "database_url_secret" {
  description = "Secret Manager secret synced to Kubernetes as DATABASE_URL"
  value       = google_secret_manager_secret.database_url.secret_id
}

output "audit_bucket" {
  description = "Retention-locked bucket receiving audit chains"
  value       = google_storage_bucket.audit.name
}

output "collector_gcp_service_account" {
  description = "Annotate the collector's service account with iam.gke.io/gcp-service-account = this value"
  value       = google_service_account.collector.email
}
