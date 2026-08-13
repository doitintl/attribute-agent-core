# =============================================================================
# Security Group Module Outputs
# =============================================================================

output "security_group_id" {
  description = "ID of the created security group"
  value       = aws_security_group.agentcore.id
}

output "security_group_arn" {
  description = "ARN of the created security group"
  value       = aws_security_group.agentcore.arn
}

output "security_group_name" {
  description = "Name of the created security group"
  value       = aws_security_group.agentcore.name
}
