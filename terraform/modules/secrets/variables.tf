# =============================================================================
# Secrets Module Variables
# =============================================================================

variable "secret_name" {
  description = "Name of the secret in Secrets Manager"
  type        = string
}

variable "sensor_token" {
  description = "DoiT Attribute sensor token"
  type        = string
  sensitive   = true
}

variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
}
