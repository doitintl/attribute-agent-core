# =============================================================================
# Security Group Module Variables
# =============================================================================

variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID where the security group will be created"
  type        = string
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}

variable "allow_http_egress" {
  description = "Allow HTTP egress for package updates"
  type        = bool
  default     = true
}
