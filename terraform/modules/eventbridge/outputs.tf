# =============================================================================
# EventBridge Module Outputs
# =============================================================================

output "rule_arn" {
  description = "ARN of the EventBridge rule"
  value       = aws_cloudwatch_event_rule.instance_launched.arn
}

output "rule_name" {
  description = "Name of the EventBridge rule"
  value       = aws_cloudwatch_event_rule.instance_launched.name
}
