# =============================================================================
# Capacity Provider Module Variables
# =============================================================================

variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
}

variable "capacity_provider_role_arn" {
  description = "ARN of the IAM role for capacity provider operator"
  type        = string
}

variable "ec2_instance_profile_arn" {
  description = "ARN of the IAM instance profile for EC2 instances (required for SSM)"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID for AgentCore instances"
  type        = string
}

variable "subnet_ids" {
  description = "Subnet IDs for AgentCore instances"
  type        = list(string)
}

variable "security_group_ids" {
  description = "Security group IDs for AgentCore instances"
  type        = list(string)
}

variable "operating_system" {
  description = "Operating system for AgentCore instances"
  type        = string
  default     = "LINUX_X86_64"
}

variable "allowed_instance_types" {
  description = "Allowed EC2 instance types"
  type        = list(string)
  default     = ["c6i.large", "c6a.large"]
}

variable "ebs_volume_size" {
  description = "EBS volume size in GB (0 to disable)"
  type        = number
  default     = 30
}

variable "ebs_volume_type" {
  description = "EBS volume type"
  type        = string
  default     = "gp3"
}

variable "capacity_min" {
  description = "Minimum capacity"
  type        = number
  default     = 0
}

variable "capacity_max" {
  description = "Maximum capacity"
  type        = number
  default     = 10
}
