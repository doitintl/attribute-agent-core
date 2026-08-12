# =============================================================================
# Lambda Module - Attribute Agent Core
# =============================================================================
# Creates the Lambda function that installs the DoiT Attribute sensor
# on AgentCore EC2 instances via SSM Run Command
# =============================================================================

# Package the Lambda function code
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = var.lambda_source_dir
  output_path = "${path.module}/lambda_function.zip"
}

# CloudWatch Log Group (created before Lambda to control retention)
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.name_prefix}-sensor-installer"
  retention_in_days = 14

  tags = {
    Name = "${var.name_prefix}-sensor-installer-logs"
  }
}

# Lambda function
resource "aws_lambda_function" "sensor_installer" {
  function_name = "${var.name_prefix}-sensor-installer"
  description   = "Installs DoiT Attribute sensor on AgentCore EC2 instances"
  
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  
  role    = var.lambda_role_arn
  handler = "index.handler"
  runtime = "python3.12"
  
  timeout     = var.lambda_timeout
  memory_size = var.lambda_memory_size

  environment {
    variables = {
      WORKLOAD_NAME    = var.workload_name
      MEMORY_LIMIT     = var.memory_limit
      SECRET_NAME      = var.secret_name
      MAX_WAIT_SECONDS = tostring(var.lambda_timeout)
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]

  tags = {
    Name = "${var.name_prefix}-sensor-installer"
  }
}
