# =============================================================================
# Variables - Attribute Agent Core
# =============================================================================

# -----------------------------------------------------------------------------
# General Configuration
# -----------------------------------------------------------------------------

variable "aws_region" {
  description = "AWS region for deployment"
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for all resource names"
  type        = string
  default     = "attribute-agentcore"
}

variable "default_tags" {
  description = "Default tags to apply to all resources"
  type        = map(string)
  default = {
    Project   = "attribute-agentcore"
    ManagedBy = "terraform"
  }
}

# -----------------------------------------------------------------------------
# Network Configuration
# -----------------------------------------------------------------------------

variable "vpc_id" {
  description = "VPC ID for AgentCore instances. Leave empty to use default VPC."
  type        = string
  default     = ""
}

variable "subnet_ids" {
  description = "Subnet IDs for AgentCore instances. Leave empty to auto-discover from VPC."
  type        = list(string)
  default     = []
}

variable "security_group_ids" {
  description = "Security group IDs for AgentCore instances. Leave empty to create a new SG or use default."
  type        = list(string)
  default     = []
}

variable "create_security_group" {
  description = "Create a dedicated security group for AgentCore instances. Ignored if security_group_ids is provided."
  type        = bool
  default     = true
}

variable "allow_http_egress" {
  description = "Allow HTTP (port 80) egress for package updates. Only applies if create_security_group is true."
  type        = bool
  default     = true
}

variable "availability_zones" {
  description = "Availability zones to use when auto-discovering subnets"
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b", "us-east-1c"]
}

# -----------------------------------------------------------------------------
# Capacity Provider Configuration
# -----------------------------------------------------------------------------

variable "operating_system" {
  description = "Operating system for AgentCore instances (LINUX_X86_64 or LINUX_ARM64)"
  type        = string
  default     = "LINUX_X86_64"

  validation {
    condition     = contains(["LINUX_X86_64", "LINUX_ARM64"], var.operating_system)
    error_message = "Operating system must be LINUX_X86_64 or LINUX_ARM64."
  }
}

variable "allowed_instance_types" {
  description = "Allowed EC2 instance types for capacity provider"
  type        = list(string)
  default     = ["c6i.large", "c6a.large", "c7i.large"]
}

variable "ebs_volume_size" {
  description = "EBS volume size in GB for AgentCore instances"
  type        = number
  default     = 30
}

variable "ebs_volume_type" {
  description = "EBS volume type for AgentCore instances"
  type        = string
  default     = "gp3"
}

variable "capacity_min" {
  description = "Minimum capacity for auto-scaling"
  type        = number
  default     = 0
}

variable "capacity_max" {
  description = "Maximum capacity for auto-scaling"
  type        = number
  default     = 10
}

# -----------------------------------------------------------------------------
# Agent Runtime Configuration
# -----------------------------------------------------------------------------

variable "build_agent_artifact" {
  description = "Build and upload the agent artifact from local source. If false, expects artifact to already exist at agent_s3_key."
  type        = bool
  default     = true
}

variable "agent_source_dir" {
  description = "Path to the agent source directory containing agent.py and requirements.txt. If empty, uses default ../agent directory."
  type        = string
  default     = ""
}

variable "agent_s3_bucket" {
  description = "S3 bucket for the agent code artifact"
  type        = string
}

variable "agent_s3_prefix" {
  description = "S3 key prefix for the agent artifact (e.g., 'weather-saas-agent'). Used when build_agent_artifact is true."
  type        = string
  default     = "agent"
}

variable "agent_artifact_name" {
  description = "Name of the agent artifact zip file (without .zip extension). Used when build_agent_artifact is true."
  type        = string
  default     = "agent"
}

variable "agent_s3_key" {
  description = "S3 key (path) to an existing agent zip file. Only used when build_agent_artifact is false."
  type        = string
  default     = "agent/agent.zip"
}

variable "agent_s3_key_prefix" {
  description = "S3 key prefix for agent artifacts (used in IAM policy). Only used when build_agent_artifact is false."
  type        = string
  default     = "agent/*"
}

variable "python_runtime" {
  description = "Python runtime version for the agent"
  type        = string
  default     = "PYTHON_3_13"

  validation {
    condition     = contains(["PYTHON_3_12", "PYTHON_3_13"], var.python_runtime)
    error_message = "Python runtime must be PYTHON_3_12 or PYTHON_3_13."
  }
}

variable "agent_entry_point" {
  description = "Entry point file for the agent"
  type        = list(string)
  default     = ["agent.py"]
}

variable "request_header_allowlist" {
  description = "List of custom headers to allow through to the agent (e.g., x-tenant-id)"
  type        = list(string)
  default     = ["x-tenant-id"]
}

variable "session_idle_timeout" {
  description = "Session idle timeout in seconds"
  type        = number
  default     = 900  # 15 minutes
}

variable "session_max_duration" {
  description = "Maximum session duration in seconds"
  type        = number
  default     = 86400  # 24 hours
}

# -----------------------------------------------------------------------------
# Attribute Sensor Configuration
# -----------------------------------------------------------------------------

variable "sensor_token" {
  description = "DoiT Attribute sensor token (sensitive)"
  type        = string
  sensitive   = true
}

variable "sensor_secret_name" {
  description = "Name of the Secrets Manager secret for the sensor token"
  type        = string
  default     = "attribute/sensor-token"
}

variable "sensor_workload_name" {
  description = "Workload name to report in Attribute dashboard"
  type        = string
  default     = "agentcore-agent"
}

variable "sensor_memory_limit" {
  description = "Memory limit for the sensor in bytes (default 500MB)"
  type        = string
  default     = "524288000"
}

# -----------------------------------------------------------------------------
# Lambda Configuration
# -----------------------------------------------------------------------------

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
