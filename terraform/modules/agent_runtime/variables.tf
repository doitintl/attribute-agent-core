# =============================================================================
# Agent Runtime Module Variables
# =============================================================================

variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
}

variable "agent_runtime_role_arn" {
  description = "ARN of the IAM role for agent runtime"
  type        = string
}

variable "capacity_provider_arn" {
  description = "ARN of the capacity provider"
  type        = string
}

variable "agent_s3_bucket" {
  description = "S3 bucket containing agent code"
  type        = string
}

variable "agent_s3_key" {
  description = "S3 key (path) to the agent zip file"
  type        = string
}

variable "python_runtime" {
  description = "Python runtime version"
  type        = string
  default     = "PYTHON_3_13"
}

variable "agent_entry_point" {
  description = "Entry point file for the agent"
  type        = list(string)
  default     = ["agent.py"]
}

variable "request_header_allowlist" {
  description = "List of custom headers to allow through to the agent"
  type        = list(string)
  default     = ["x-tenant-id"]
}

variable "session_idle_timeout" {
  description = "Session idle timeout in seconds"
  type        = number
  default     = 900
}

variable "session_max_duration" {
  description = "Maximum session duration in seconds"
  type        = number
  default     = 86400
}

variable "environment_variables" {
  description = "Environment variables to pass to the agent runtime"
  type        = map(string)
  default     = {}
}