terraform {
  # required_version + required_providers come from shared-providers.tf,
  # which the tofu-plan-apply-template workflow overlays into this dir
  # from nelsong6/infra-bootstrap/tofu/provider/.
  backend "azurerm" {
    use_oidc = true
  }
}
