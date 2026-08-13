# =============================================================================
# Security Group Module - Attribute Agent Core
# =============================================================================
# Creates security group for AgentCore EC2 instances with required egress rules
# =============================================================================

resource "aws_security_group" "agentcore" {
  name        = "${var.name_prefix}-agentcore-sg"
  description = "Security group for AgentCore EC2 instances"
  vpc_id      = var.vpc_id

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-agentcore-sg"
  })
}

# -----------------------------------------------------------------------------
# Egress Rules - AgentCore instances need outbound access
# -----------------------------------------------------------------------------

# HTTPS for AWS APIs (Bedrock, S3, Secrets Manager, SSM, CloudWatch)
resource "aws_security_group_rule" "egress_https" {
  type              = "egress"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  cidr_blocks       = ["0.0.0.0/0"]
  security_group_id = aws_security_group.agentcore.id
  description       = "HTTPS for AWS APIs (Bedrock, S3, SSM, CloudWatch)"
}

# DNS resolution
resource "aws_security_group_rule" "egress_dns_udp" {
  type              = "egress"
  from_port         = 53
  to_port           = 53
  protocol          = "udp"
  cidr_blocks       = ["0.0.0.0/0"]
  security_group_id = aws_security_group.agentcore.id
  description       = "DNS resolution (UDP)"
}

resource "aws_security_group_rule" "egress_dns_tcp" {
  type              = "egress"
  from_port         = 53
  to_port           = 53
  protocol          = "tcp"
  cidr_blocks       = ["0.0.0.0/0"]
  security_group_id = aws_security_group.agentcore.id
  description       = "DNS resolution (TCP)"
}

# HTTP for package updates (yum/apt) - optional but useful
resource "aws_security_group_rule" "egress_http" {
  count             = var.allow_http_egress ? 1 : 0
  type              = "egress"
  from_port         = 80
  to_port           = 80
  protocol          = "tcp"
  cidr_blocks       = ["0.0.0.0/0"]
  security_group_id = aws_security_group.agentcore.id
  description       = "HTTP for package updates"
}

# -----------------------------------------------------------------------------
# No inbound rules by default
# AgentCore manages instance traffic internally via its control plane
# -----------------------------------------------------------------------------
