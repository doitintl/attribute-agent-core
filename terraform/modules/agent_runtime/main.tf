# =============================================================================
# Agent Runtime Module - Attribute Agent Core
# =============================================================================
# Creates AgentCore runtime using AWS CLI (no native TF resource yet)
# Deploys agent code from S3 with custom header allowlist
# =============================================================================

# Generate a unique suffix for the runtime name
resource "random_id" "suffix" {
  byte_length = 4
}

locals {
  runtime_name = "${replace(var.name_prefix, "-", "_")}_runtime_${random_id.suffix.hex}"
  
  # Build artifact configuration
  artifact_config = {
    codeConfiguration = {
      code = {
        s3 = {
          bucket = var.agent_s3_bucket
          prefix = var.agent_s3_key
        }
      }
      runtime    = var.python_runtime
      entryPoint = var.agent_entry_point
    }
  }
  
  # Build capacity provider configuration
  capacity_config = {
    capacityProviderArn = var.capacity_provider_arn
  }
  
  # Build header configuration (only if headers are specified)
  header_config = length(var.request_header_allowlist) > 0 ? {
    requestHeaderAllowlist = var.request_header_allowlist
  } : null
}

# Write JSON configs to files (avoids shell quoting issues)
resource "local_file" "artifact_config" {
  content  = jsonencode(local.artifact_config)
  filename = "${path.module}/.terraform/artifact_${local.runtime_name}.json"
}

resource "local_file" "capacity_config" {
  content  = jsonencode(local.capacity_config)
  filename = "${path.module}/.terraform/capacity_${local.runtime_name}.json"
}

resource "local_file" "header_config" {
  count    = local.header_config != null ? 1 : 0
  content  = jsonencode(local.header_config)
  filename = "${path.module}/.terraform/header_${local.runtime_name}.json"
}

# Create agent runtime
resource "null_resource" "agent_runtime" {
  depends_on = [local_file.artifact_config, local_file.capacity_config, local_file.header_config]
  
  triggers = {
    name                 = local.runtime_name
    region               = data.aws_region.current.region
    role_arn             = var.agent_runtime_role_arn
    capacity_provider    = var.capacity_provider_arn
    s3_bucket            = var.agent_s3_bucket
    s3_key               = var.agent_s3_key
    python_runtime       = var.python_runtime
    entry_point          = join(",", var.agent_entry_point)
    header_allowlist     = join(",", var.request_header_allowlist)
    # File paths for cleanup
    artifact_file        = local_file.artifact_config.filename
    capacity_file        = local_file.capacity_config.filename
    header_file          = length(local_file.header_config) > 0 ? local_file.header_config[0].filename : ""
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      
      echo "Creating agent runtime ${local.runtime_name}..."
      
      # Check if runtime already exists (by prefix match)
      EXISTING_ID=$(aws bedrock-agentcore-control list-agent-runtimes \
        --region ${data.aws_region.current.region} \
        --output json | jq -r '[.agentRuntimes[] | select(.agentRuntimeId | startswith("${local.runtime_name}"))] | .[0].agentRuntimeId // empty')
      
      if [ -n "$EXISTING_ID" ]; then
        echo "Runtime already exists: $EXISTING_ID"
        echo "$EXISTING_ID" > /tmp/agent_runtime_${local.runtime_name}_id.txt
        
        # Wait for it to be READY if needed
        STATUS=$(aws bedrock-agentcore-control get-agent-runtime \
          --agent-runtime-id "$EXISTING_ID" \
          --region ${data.aws_region.current.region} \
          --query 'status' --output text)
        
        if [ "$STATUS" = "READY" ]; then
          echo "Runtime is already READY"
          exit 0
        fi
      fi
      
      # Read JSON configs from files
      ARTIFACT_JSON=$(cat "${local_file.artifact_config.filename}")
      CAPACITY_JSON=$(cat "${local_file.capacity_config.filename}")
      
      # Build and execute the create command
      %{if local.header_config != null}
      HEADER_JSON=$(cat "${local_file.header_config[0].filename}")
      RESPONSE=$(aws bedrock-agentcore-control create-agent-runtime \
        --agent-runtime-name '${local.runtime_name}' \
        --role-arn '${var.agent_runtime_role_arn}' \
        --agent-runtime-artifact "$ARTIFACT_JSON" \
        --capacity-provider-configuration "$CAPACITY_JSON" \
        --request-header-configuration "$HEADER_JSON" \
        --region ${data.aws_region.current.region} \
        --output json)
      %{else}
      RESPONSE=$(aws bedrock-agentcore-control create-agent-runtime \
        --agent-runtime-name '${local.runtime_name}' \
        --role-arn '${var.agent_runtime_role_arn}' \
        --agent-runtime-artifact "$ARTIFACT_JSON" \
        --capacity-provider-configuration "$CAPACITY_JSON" \
        --region ${data.aws_region.current.region} \
        --output json)
      %{endif}
      
      echo "$RESPONSE" > /tmp/agent_runtime_${local.runtime_name}.json
      
      # Extract the actual runtime ID (AWS adds a suffix)
      ACTUAL_RUNTIME_ID=$(echo "$RESPONSE" | jq -r '.agentRuntimeId')
      echo "Actual Agent Runtime ID: $ACTUAL_RUNTIME_ID"
      echo "$ACTUAL_RUNTIME_ID" > /tmp/agent_runtime_${local.runtime_name}_id.txt
      
      echo "Waiting for agent runtime to become READY..."
      for i in {1..120}; do
        STATUS=$(aws bedrock-agentcore-control get-agent-runtime \
          --agent-runtime-id "$ACTUAL_RUNTIME_ID" \
          --region ${data.aws_region.current.region} \
          --query 'status' --output text 2>/dev/null || echo "CREATING")
        
        if [ "$STATUS" = "READY" ]; then
          echo "Agent runtime is READY"
          exit 0
        fi
        
        if [ "$STATUS" = "CREATE_FAILED" ] || [ "$STATUS" = "FAILED" ]; then
          echo "ERROR: Agent runtime creation failed"
          aws bedrock-agentcore-control get-agent-runtime \
            --agent-runtime-id "$ACTUAL_RUNTIME_ID" \
            --region ${data.aws_region.current.region} 2>/dev/null || true
          exit 1
        fi
        
        echo "Status: $STATUS, waiting... ($i/120)"
        sleep 10
      done
      
      echo "ERROR: Timeout waiting for agent runtime"
      exit 1
    EOT
  }

  provisioner "local-exec" {
    when    = destroy
    command = <<-EOT
      # Find the actual runtime ID by prefix match
      ACTUAL_RUNTIME_ID=$(aws bedrock-agentcore-control list-agent-runtimes \
        --region "${self.triggers.region}" \
        --output json | jq -r '.agentRuntimes[] | select(.agentRuntimeId | startswith("${self.triggers.name}")) | .agentRuntimeId' | head -1)
      
      if [ -n "$ACTUAL_RUNTIME_ID" ]; then
        echo "Deleting agent runtime: $ACTUAL_RUNTIME_ID"
        aws bedrock-agentcore-control delete-agent-runtime \
          --agent-runtime-id "$ACTUAL_RUNTIME_ID" \
          --region "${self.triggers.region}" || true
      else
        echo "Agent runtime not found, may have been deleted already"
      fi
    EOT
  }
}

# Data source to get current region
data "aws_region" "current" {}

# Fetch runtime details after creation (using temp file for actual ID)
data "external" "agent_runtime_info" {
  depends_on = [null_resource.agent_runtime]
  
  program = ["bash", "-c", <<-EOT
    # Primary: use temp file which has the actual ID from create response
    if [ -f "/tmp/agent_runtime_${local.runtime_name}_id.txt" ]; then
      ACTUAL_ID=$(cat /tmp/agent_runtime_${local.runtime_name}_id.txt)
      RESULT=$(aws bedrock-agentcore-control get-agent-runtime \
        --agent-runtime-id "$ACTUAL_ID" \
        --region ${data.aws_region.current.region} \
        --output json 2>/dev/null || echo '{}')
    else
      # Fallback: find by prefix match (gets first match)
      RESULT=$(aws bedrock-agentcore-control list-agent-runtimes \
        --region ${data.aws_region.current.region} \
        --output json | jq '[.agentRuntimes[] | select(.agentRuntimeId | startswith("${local.runtime_name}"))] | .[0] // {}')
    fi
    
    ARN=$(echo "$RESULT" | jq -r '.agentRuntimeArn // empty')
    ID=$(echo "$RESULT" | jq -r '.agentRuntimeId // empty')
    STATUS=$(echo "$RESULT" | jq -r '.status // empty')
    VERSION=$(echo "$RESULT" | jq -r '.agentRuntimeVersion // empty')
    
    if [ -z "$ARN" ]; then
      ARN="arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:runtime/${local.runtime_name}"
    fi
    
    if [ -z "$ID" ]; then
      ID="${local.runtime_name}"
    fi
    
    echo "{\"arn\":\"$ARN\",\"id\":\"$ID\",\"status\":\"$STATUS\",\"version\":\"$VERSION\"}"
  EOT
  ]
}

data "aws_caller_identity" "current" {}
