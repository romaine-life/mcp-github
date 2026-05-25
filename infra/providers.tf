provider "azurerm" {
  features {}
  use_oidc = true
  # subscription_id / tenant_id come from the ARM_* env vars the shared
  # tofu workflow exports for OIDC auth.
  resource_provider_registrations = "none"
}
