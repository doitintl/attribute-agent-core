# =============================================================================
# Attribute Agent Core - Main Terraform Configuration
# =============================================================================
# This module deploys the DoiT Attribute sensor auto-deployment infrastructure
# for Amazon Bedrock AgentCore Runtime Instances.
#
# Components:
# - AgentCore Capacity Provider (EC2-based agent execution)
# - AgentCore Runtime (agent code deployment)
# - EventBridge Rule (detect instance launches)
# - Lambda Function (install sensor via SSM)
# - Secrets Manager (store sensor token)
# - IAM Roles (capacity provider operator, Lambda execution)
# =============================================================================

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.50.0"
    }
    awscc = {
      source  = "hashicorp/awscc"
      version = ">= 1.0.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = var.default_tags
  }
}

provider "awscc" {
  region = var.aws_region
}

# =============================================================================
# Data Sources
# =============================================================================

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Get default VPC if vpc_id not specified
data "aws_vpc" "default" {
  count   = var.vpc_id == "" ? 1 : 0
  default = true
}

# Get subnets for the VPC
data "aws_subnets" "selected" {
  filter {
    name   = "vpc-id"
    values = [local.vpc_id]
  }

  filter {
    name   = "availability-zone"
    values = var.availability_zones
  }
}

# Get default security group if not specified and create_security_group is false
data "aws_security_group" "default" {
  count  = length(var.security_group_ids) == 0 && !var.create_security_group ? 1 : 0
  vpc_id = local.vpc_id

  filter {
    name   = "group-name"
    values = ["default"]
  }
}

# =============================================================================
# Locals
# =============================================================================

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region

  # Use provided VPC or default
  vpc_id = var.vpc_id != "" ? var.vpc_id : data.aws_vpc.default[0].id

  # Security group priority:
  # 1. Explicitly provided security_group_ids
  # 2. Created by this module if create_security_group = true
  # 3. Default VPC security group
  security_group_ids = length(var.security_group_ids) > 0 ? var.security_group_ids : (
    var.create_security_group ? [module.security_group[0].security_group_id] : [data.aws_security_group.default[0].id]
  )

  # Use provided subnets or discovered subnets
  subnet_ids = length(var.subnet_ids) > 0 ? var.subnet_ids : data.aws_subnets.selected.ids

  # Resource naming
  name_prefix = var.name_prefix

  # Lambda package
  lambda_source_dir = "${path.module}/../lambda"

  # Agent artifact S3 key - use built artifact or provided key
  agent_s3_key = var.build_agent_artifact ? "${var.agent_s3_prefix}/${var.agent_artifact_name}.zip" : var.agent_s3_key
  
  # Agent S3 key prefix for IAM policy
  agent_s3_key_prefix = var.build_agent_artifact ? "${var.agent_s3_prefix}/*" : var.agent_s3_key_prefix
}

# =============================================================================
# Modules
# =============================================================================

# Security Group (created if no security_group_ids provided and create_security_group = true)
module "security_group" {
  count  = var.create_security_group && length(var.security_group_ids) == 0 ? 1 : 0
  source = "./modules/security_group"

  name_prefix       = local.name_prefix
  vpc_id            = local.vpc_id
  allow_http_egress = var.allow_http_egress
  tags              = var.default_tags
}

# Agent Artifact - builds and uploads agent code to S3
module "agent_artifact" {
  count  = var.build_agent_artifact ? 1 : 0
  source = "./modules/agent_artifact"

  s3_bucket        = var.agent_s3_bucket
  s3_prefix        = var.agent_s3_prefix
  artifact_name    = var.agent_artifact_name
  agent_source_dir = var.agent_source_dir
  tags             = var.default_tags
}

module "iam" {
  source = "./modules/iam"

  name_prefix         = local.name_prefix
  account_id          = local.account_id
  region              = local.region
  secret_name         = var.sensor_secret_name
  agent_s3_bucket     = var.agent_s3_bucket
  agent_s3_key_prefix = local.agent_s3_key_prefix
}

module "secrets" {
  source = "./modules/secrets"

  secret_name  = var.sensor_secret_name
  sensor_token = var.sensor_token
  name_prefix  = local.name_prefix
}

module "lambda" {
  source = "./modules/lambda"

  name_prefix        = local.name_prefix
  lambda_role_arn    = module.iam.lambda_execution_role_arn
  workload_name      = var.sensor_workload_name
  memory_limit       = var.sensor_memory_limit
  secret_name        = var.sensor_secret_name
  lambda_source_dir  = local.lambda_source_dir
  lambda_timeout     = var.lambda_timeout
  lambda_memory_size = var.lambda_memory_size
}

module "eventbridge" {
  source = "./modules/eventbridge"

  name_prefix         = local.name_prefix
  lambda_function_arn = module.lambda.function_arn
  lambda_function_name = module.lambda.function_name
}

module "capacity_provider" {
  source = "./modules/capacity_provider"

  name_prefix                = local.name_prefix
  capacity_provider_role_arn = module.iam.capacity_provider_role_arn
  ec2_instance_profile_arn   = module.iam.ec2_instance_profile_arn
  vpc_id                     = local.vpc_id
  subnet_ids                 = local.subnet_ids
  security_group_ids         = local.security_group_ids
  operating_system           = var.operating_system
  allowed_instance_types     = var.allowed_instance_types
  ebs_volume_size            = var.ebs_volume_size
  ebs_volume_type            = var.ebs_volume_type
  capacity_min               = var.capacity_min
  capacity_max               = var.capacity_max
}

module "agent_runtime" {
  source = "./modules/agent_runtime"

  name_prefix               = local.name_prefix
  agent_runtime_role_arn    = module.iam.agent_runtime_role_arn
  capacity_provider_arn     = module.capacity_provider.capacity_provider_arn
  agent_s3_bucket           = var.agent_s3_bucket
  agent_s3_key              = local.agent_s3_key
  python_runtime            = var.python_runtime
  agent_entry_point         = var.agent_entry_point
  request_header_allowlist  = var.request_header_allowlist
  session_idle_timeout      = var.session_idle_timeout
  session_max_duration      = var.session_max_duration

  depends_on = [module.capacity_provider, module.agent_artifact]
}
