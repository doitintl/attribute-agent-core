# =============================================================================
# IAM Module Variables
# =============================================================================

variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
}

variable "account_id" {
  description = "AWS account ID"
  type        = string
}

variable "region" {
  description = "AWS region"
  type        = string
}

variable "secret_name" {
  description = "Name of the Secrets Manager secret for sensor token"
  type        = string
}

variable "agent_s3_bucket" {
  description = "S3 bucket containing agent code"
  type        = string
}

variable "agent_s3_key_prefix" {
  description = "S3 key prefix for agent artifacts"
  type        = string
}
