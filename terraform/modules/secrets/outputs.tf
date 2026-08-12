# =============================================================================
# Secrets Module Outputs
# =============================================================================

output "secret_arn" {
  description = "ARN of the sensor token secret"
  value       = aws_secretsmanager_secret.sensor_token.arn
}

output "secret_name" {
  description = "Name of the sensor token secret"
  value       = aws_secretsmanager_secret.sensor_token.name
}
