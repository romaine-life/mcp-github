variable "key_vault_name" {
  description = "MCP GitHub-owned Key Vault for GitHub App credentials."
  type        = string
  default     = "ng6-mcp-github"
}

variable "resource_group_name" {
  description = "Resource group for mcp-github infrastructure."
  type        = string
  default     = "infra"
}
