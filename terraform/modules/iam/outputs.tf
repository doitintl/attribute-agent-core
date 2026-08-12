# =============================================================================
# IAM Module Outputs
# =============================================================================

output "capacity_provider_role_arn" {
  description = "ARN of the capacity provider operator IAM role"
  value       = aws_iam_role.capacity_provider.arn
}

output "capacity_provider_role_name" {
  description = "Name of the capacity provider operator IAM role"
  value       = aws_iam_role.capacity_provider.name
}

output "lambda_execution_role_arn" {
  description = "ARN of the Lambda execution IAM role"
  value       = aws_iam_role.lambda_execution.arn
}

output "lambda_execution_role_name" {
  description = "Name of the Lambda execution IAM role"
  value       = aws_iam_role.lambda_execution.name
}

output "agent_runtime_role_arn" {
  description = "ARN of the agent runtime IAM role"
  value       = aws_iam_role.agent_runtime.arn
}

output "agent_runtime_role_name" {
  description = "Name of the agent runtime IAM role"
  value       = aws_iam_role.agent_runtime.name
}

output "ec2_instance_profile_arn" {
  description = "ARN of the EC2 instance profile for AgentCore instances"
  value       = aws_iam_instance_profile.ec2_instance.arn
}

output "ec2_instance_profile_name" {
  description = "Name of the EC2 instance profile for AgentCore instances"
  value       = aws_iam_instance_profile.ec2_instance.name
}
