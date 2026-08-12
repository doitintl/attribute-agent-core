# =============================================================================
# Lambda Module Variables
# =============================================================================

variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
}

variable "lambda_role_arn" {
  description = "ARN of the IAM role for Lambda execution"
  type        = string
}

variable "workload_name" {
  description = "Workload name to report in Attribute dashboard"
  type        = string
}

variable "memory_limit" {
  description = "Memory limit for the sensor in bytes"
  type        = string
}

variable "secret_name" {
  description = "Name of the Secrets Manager secret for sensor token"
  type        = string
}

variable "lambda_source_dir" {
  description = "Directory containing Lambda function code"
  type        = string
}

variable "lambda_timeout" {
  description = "Lambda function timeout in seconds"
  type        = number
  default     = 300
}

variable "lambda_memory_size" {
  description = "Lambda function memory size in MB"
  type        = number
  default     = 256
}
