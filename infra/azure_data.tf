# Shared Azure resources live in infra-bootstrap; this app stack only reads
# enough to create its own runtime Key Vault and grant External Secrets access.

data "azurerm_client_config" "current" {}

data "azurerm_resource_group" "main" {
  name = var.resource_group_name
}

data "azurerm_user_assigned_identity" "external_secrets" {
  name                = "infra-shared-identity"
  resource_group_name = data.azurerm_resource_group.main.name
}
