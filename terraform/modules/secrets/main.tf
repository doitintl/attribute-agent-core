# =============================================================================
# Secrets Module - Attribute Agent Core
# =============================================================================
# Stores the DoiT Attribute sensor token in AWS Secrets Manager
# =============================================================================

# Random suffix to avoid conflicts with secrets scheduled for deletion
resource "random_id" "secret_suffix" {
  byte_length = 3
}

resource "aws_secretsmanager_secret" "sensor_token" {
  name        = "${var.secret_name}-${random_id.secret_suffix.hex}"
  description = "DoiT Attribute sensor token for AgentCore instances"

  tags = {
    Name = "${var.name_prefix}-sensor-token"
  }
}

resource "aws_secretsmanager_secret_version" "sensor_token" {
  secret_id = aws_secretsmanager_secret.sensor_token.id
  
  secret_string = jsonencode({
    token = var.sensor_token
  })
}
