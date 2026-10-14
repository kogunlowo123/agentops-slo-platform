output "workload_identity_client_id" {
  description = "Annotate the service account with azure.workload.identity/client-id = this value"
  value       = azurerm_user_assigned_identity.agentops.client_id
}

output "key_vault_uri" {
  description = "Key Vault holding the database URL and audit signing-key secrets"
  value       = azurerm_key_vault.agentops.vault_uri
}

output "audit_container" {
  description = "Immutable blob container receiving audit chains"
  value       = "${azurerm_storage_account.audit.name}/${azurerm_storage_container.audit.name}"
}
