# =============================================================================
# Outputs - Attribute Agent Core
# =============================================================================

# -----------------------------------------------------------------------------
# Capacity Provider
# -----------------------------------------------------------------------------

output "capacity_provider_arn" {
  description = "ARN of the AgentCore capacity provider"
  value       = module.capacity_provider.capacity_provider_arn
}

output "capacity_provider_id" {
  description = "ID of the AgentCore capacity provider"
  value       = module.capacity_provider.capacity_provider_id
}

# -----------------------------------------------------------------------------
# Agent Runtime
# -----------------------------------------------------------------------------

output "agent_runtime_arn" {
  description = "ARN of the AgentCore runtime"
  value       = module.agent_runtime.agent_runtime_arn
}

output "agent_runtime_id" {
  description = "ID of the AgentCore runtime"
  value       = module.agent_runtime.agent_runtime_id
}

# -----------------------------------------------------------------------------
# Lambda
# -----------------------------------------------------------------------------

output "lambda_function_arn" {
  description = "ARN of the sensor installer Lambda function"
  value       = module.lambda.function_arn
}

output "lambda_function_name" {
  description = "Name of the sensor installer Lambda function"
  value       = module.lambda.function_name
}

output "lambda_log_group" {
  description = "CloudWatch log group for the Lambda function"
  value       = module.lambda.log_group_name
}

# -----------------------------------------------------------------------------
# EventBridge
# -----------------------------------------------------------------------------

output "eventbridge_rule_arn" {
  description = "ARN of the EventBridge rule"
  value       = module.eventbridge.rule_arn
}

output "eventbridge_rule_name" {
  description = "Name of the EventBridge rule"
  value       = module.eventbridge.rule_name
}

# -----------------------------------------------------------------------------
# IAM
# -----------------------------------------------------------------------------

output "capacity_provider_role_arn" {
  description = "ARN of the capacity provider operator IAM role"
  value       = module.iam.capacity_provider_role_arn
}

output "lambda_execution_role_arn" {
  description = "ARN of the Lambda execution IAM role"
  value       = module.iam.lambda_execution_role_arn
}

output "agent_runtime_role_arn" {
  description = "ARN of the agent runtime IAM role"
  value       = module.iam.agent_runtime_role_arn
}

# -----------------------------------------------------------------------------
# Secrets
# -----------------------------------------------------------------------------

output "sensor_secret_arn" {
  description = "ARN of the sensor token secret in Secrets Manager"
  value       = module.secrets.secret_arn
}

# -----------------------------------------------------------------------------
# Agent Artifact
# -----------------------------------------------------------------------------

output "agent_artifact_s3_uri" {
  description = "S3 URI of the agent artifact"
  value       = var.build_agent_artifact ? module.agent_artifact[0].s3_uri : "s3://${var.agent_s3_bucket}/${var.agent_s3_key}"
}

output "agent_artifact_s3_key" {
  description = "S3 key of the agent artifact"
  value       = local.agent_s3_key
}

# -----------------------------------------------------------------------------
# Network
# -----------------------------------------------------------------------------

output "vpc_id" {
  description = "VPC ID used for AgentCore instances"
  value       = local.vpc_id
}

output "subnet_ids" {
  description = "Subnet IDs used for AgentCore instances"
  value       = local.subnet_ids
}

output "security_group_ids" {
  description = "Security group IDs used for AgentCore instances"
  value       = local.security_group_ids
}

output "created_security_group_id" {
  description = "ID of the security group created by this module (null if using provided SG)"
  value       = var.create_security_group && length(var.security_group_ids) == 0 ? module.security_group[0].security_group_id : null
}

# -----------------------------------------------------------------------------
# Quick Reference
# -----------------------------------------------------------------------------

output "invoke_command" {
  description = "AWS CLI command to invoke the agent"
  value       = <<-EOT
    aws bedrock-agentcore invoke-agent-runtime \
      --agent-runtime-arn ${module.agent_runtime.agent_runtime_arn} \
      --qualifier DEFAULT \
      --runtime-session-id "test-session-$(uuidgen | tr -d '-')" \
      --payload '{"prompt": "What is the weather in Seattle?"}' \
      --content-type application/json \
      --region ${var.aws_region}
  EOT
}
