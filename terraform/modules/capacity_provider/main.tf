# =============================================================================
# Capacity Provider Module - Attribute Agent Core
# =============================================================================
# Creates AgentCore capacity provider using AWS CLI (no native TF resource yet)
# Uses null_resource with local-exec for create/destroy lifecycle
# =============================================================================

# Generate a unique suffix for the capacity provider name
resource "random_id" "suffix" {
  byte_length = 4
}

locals {
  capacity_provider_name = "${replace(var.name_prefix, "-", "_")}_cp_${random_id.suffix.hex}"
  
  # Build compute configuration JSON
  compute_config = {
    ec2Configuration = {
      launchTemplateSource = {
        launchParameters = {
          operatingSystem = var.operating_system
          instanceRequirements = {
            allowedInstanceTypes = var.allowed_instance_types
          }
          instanceProfileArn = var.ec2_instance_profile_arn
        }
      }
      vpcConfiguration = {
        subnets        = var.subnet_ids
        securityGroups = var.security_group_ids
      }
      volumes = var.ebs_volume_size > 0 ? [
        {
          ebsConfiguration = {
            name       = "data"
            sizeGiB    = var.ebs_volume_size
            volumeType = var.ebs_volume_type
          }
        }
      ] : []
    }
  }
  
  permissions_config = {
    capacityProviderOperatorRoleArn = var.capacity_provider_role_arn
  }
}

# Create capacity provider
resource "null_resource" "capacity_provider" {
  triggers = {
    name                 = local.capacity_provider_name
    region               = data.aws_region.current.region
    role_arn             = var.capacity_provider_role_arn
    instance_profile_arn = var.ec2_instance_profile_arn
    operating_system     = var.operating_system
    instance_types       = join(",", var.allowed_instance_types)
    subnets              = join(",", var.subnet_ids)
    security_groups      = join(",", var.security_group_ids)
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      
      # Wait for IAM role to propagate
      echo "Waiting 15 seconds for IAM role propagation..."
      sleep 15
      
      # Create capacity provider and capture the response
      echo "Creating capacity provider ${local.capacity_provider_name}..."
      RESPONSE=$(aws bedrock-agentcore-control create-capacity-provider \
        --name "${local.capacity_provider_name}" \
        --permissions-configuration '${jsonencode(local.permissions_config)}' \
        --compute-configuration '${jsonencode(local.compute_config)}' \
        --region ${data.aws_region.current.region} \
        --output json)
      
      echo "$RESPONSE" > /tmp/capacity_provider_${local.capacity_provider_name}.json
      
      # Extract the actual capacity provider ID (AWS adds a suffix)
      ACTUAL_CP_ID=$(echo "$RESPONSE" | jq -r '.capacityProviderId')
      echo "Actual Capacity Provider ID: $ACTUAL_CP_ID"
      echo "$ACTUAL_CP_ID" > /tmp/capacity_provider_${local.capacity_provider_name}_id.txt
      
      echo "Waiting for capacity provider to become READY..."
      for i in {1..90}; do
        STATUS=$(aws bedrock-agentcore-control get-capacity-provider \
          --capacity-provider-id "$ACTUAL_CP_ID" \
          --region ${data.aws_region.current.region} \
          --query 'status' --output text 2>/dev/null || echo "CREATING")
        
        if [ "$STATUS" = "READY" ]; then
          echo "Capacity provider is READY"
          exit 0
        fi
        
        if [ "$STATUS" = "CREATE_FAILED" ] || [ "$STATUS" = "FAILED" ]; then
          echo "ERROR: Capacity provider creation failed"
          aws bedrock-agentcore-control get-capacity-provider \
            --capacity-provider-id "$ACTUAL_CP_ID" \
            --region ${data.aws_region.current.region} 2>/dev/null || true
          exit 1
        fi
        
        echo "Status: $STATUS, waiting... ($i/90)"
        sleep 10
      done
      
      echo "ERROR: Timeout waiting for capacity provider"
      exit 1
    EOT
  }

  provisioner "local-exec" {
    when    = destroy
    command = <<-EOT
      # Find the actual capacity provider ID by prefix match
      ACTUAL_CP_ID=$(aws bedrock-agentcore-control list-capacity-providers \
        --region "${self.triggers.region}" \
        --output json | jq -r '.capacityProviders[] | select(.capacityProviderId | startswith("${self.triggers.name}")) | .capacityProviderId' | head -1)
      
      if [ -n "$ACTUAL_CP_ID" ]; then
        echo "Deleting capacity provider: $ACTUAL_CP_ID"
        aws bedrock-agentcore-control delete-capacity-provider \
          --capacity-provider-id "$ACTUAL_CP_ID" \
          --region "${self.triggers.region}" || true
      else
        echo "Capacity provider not found, may have been deleted already"
      fi
    EOT
  }
}

# Data source to get current region
data "aws_region" "current" {}

# Fetch capacity provider details after creation (using temp file for actual ID)
data "external" "capacity_provider_info" {
  depends_on = [null_resource.capacity_provider]
  
  program = ["bash", "-c", <<-EOT
    # Primary: use temp file which has the actual ID from create response
    if [ -f "/tmp/capacity_provider_${local.capacity_provider_name}_id.txt" ]; then
      ACTUAL_ID=$(cat /tmp/capacity_provider_${local.capacity_provider_name}_id.txt)
      RESULT=$(aws bedrock-agentcore-control get-capacity-provider \
        --capacity-provider-id "$ACTUAL_ID" \
        --region ${data.aws_region.current.region} \
        --output json 2>/dev/null || echo '{}')
    else
      # Fallback: find by prefix match (gets first match)
      RESULT=$(aws bedrock-agentcore-control list-capacity-providers \
        --region ${data.aws_region.current.region} \
        --output json | jq '[.capacityProviders[] | select(.capacityProviderId | startswith("${local.capacity_provider_name}"))] | .[0] // {}')
    fi
    
    ARN=$(echo "$RESULT" | jq -r '.capacityProviderArn // empty')
    ID=$(echo "$RESULT" | jq -r '.capacityProviderId // empty')
    STATUS=$(echo "$RESULT" | jq -r '.status // empty')
    
    if [ -z "$ARN" ]; then
      ARN="arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:capacity-provider/${local.capacity_provider_name}"
    fi
    
    if [ -z "$ID" ]; then
      ID="${local.capacity_provider_name}"
    fi
    
    echo "{\"arn\":\"$ARN\",\"id\":\"$ID\",\"status\":\"$STATUS\"}"
  EOT
  ]
}

data "aws_caller_identity" "current" {}
