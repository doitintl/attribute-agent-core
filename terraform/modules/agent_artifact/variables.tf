# =============================================================================
# Agent Artifact Module - Variables
# =============================================================================

variable "s3_bucket" {
  description = "S3 bucket to upload the agent artifact to"
  type        = string
}

variable "s3_prefix" {
  description = "S3 key prefix for the artifact (e.g., 'weather-saas-agent')"
  type        = string
  default     = "agent"
}

variable "artifact_name" {
  description = "Name of the artifact zip file (without .zip extension)"
  type        = string
  default     = "agent"
}

variable "agent_source_dir" {
  description = "Path to the agent source directory containing agent.py and requirements.txt. If empty, uses the default ../../../agent directory."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}
