# =============================================================================
# Lambda Module Outputs
# =============================================================================

output "function_arn" {
  description = "ARN of the Lambda function"
  value       = aws_lambda_function.sensor_installer.arn
}

output "function_name" {
  description = "Name of the Lambda function"
  value       = aws_lambda_function.sensor_installer.function_name
}

output "invoke_arn" {
  description = "Invoke ARN of the Lambda function"
  value       = aws_lambda_function.sensor_installer.invoke_arn
}

output "log_group_name" {
  description = "Name of the CloudWatch log group"
  value       = aws_cloudwatch_log_group.lambda.name
}

output "log_group_arn" {
  description = "ARN of the CloudWatch log group"
  value       = aws_cloudwatch_log_group.lambda.arn
}
