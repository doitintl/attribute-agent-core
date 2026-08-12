# =============================================================================
# EventBridge Module - Attribute Agent Core
# =============================================================================
# Creates EventBridge rule to trigger Lambda when EC2 instances enter
# 'running' state (AgentCore instance launches)
# =============================================================================

# EventBridge rule for EC2 state changes
resource "aws_cloudwatch_event_rule" "instance_launched" {
  name        = "${var.name_prefix}-instance-launched"
  description = "Triggers sensor installation when AgentCore instances launch"

  event_pattern = jsonencode({
    source      = ["aws.ec2"]
    detail-type = ["EC2 Instance State-change Notification"]
    detail = {
      state = ["running"]
    }
  })

  tags = {
    Name = "${var.name_prefix}-instance-launched"
  }
}

# Lambda permission to allow EventBridge invocation
resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = var.lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.instance_launched.arn
}

# EventBridge target to Lambda
resource "aws_cloudwatch_event_target" "lambda" {
  rule      = aws_cloudwatch_event_rule.instance_launched.name
  target_id = "SensorInstallerLambda"
  arn       = var.lambda_function_arn
}
