# =============================================================================
# Agent Artifact Module
# =============================================================================
# Builds and uploads the agent artifact to S3
# - Installs Python dependencies from requirements.txt
# - Packages agent code into a zip file
# - Uploads to the specified S3 bucket
# =============================================================================

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0"
    }
  }
}

# -----------------------------------------------------------------------------
# Data Sources
# -----------------------------------------------------------------------------

data "aws_region" "current" {}

# -----------------------------------------------------------------------------
# Local Values
# -----------------------------------------------------------------------------

locals {
  # Hash the source files to detect changes
  agent_source_hash = sha256(join("", [
    filesha256(var.agent_source_dir != "" ? "${var.agent_source_dir}/agent.py" : "${path.module}/../../../agent/agent.py"),
    filesha256(var.agent_source_dir != "" ? "${var.agent_source_dir}/requirements.txt" : "${path.module}/../../../agent/requirements.txt"),
  ]))
  
  # Build directory for packaging
  build_dir = "${path.module}/.build"
  
  # Output zip file
  zip_file = "${local.build_dir}/${var.artifact_name}.zip"
  
  # S3 key for the artifact
  s3_key = "${var.s3_prefix}/${var.artifact_name}.zip"
  
  # Source directory (use provided or default to ../../../agent)
  source_dir = var.agent_source_dir != "" ? var.agent_source_dir : "${path.module}/../../../agent"
}

# -----------------------------------------------------------------------------
# Build and Package Agent
# -----------------------------------------------------------------------------

resource "null_resource" "build_agent" {
  triggers = {
    source_hash = local.agent_source_hash
    s3_bucket   = var.s3_bucket
    s3_key      = local.s3_key
  }

  provisioner "local-exec" {
    command = <<-EOF
      set -e
      
      echo "=== Building agent artifact ==="
      
      # Create build directory
      BUILD_DIR="${local.build_dir}"
      rm -rf "$BUILD_DIR"
      mkdir -p "$BUILD_DIR/package"
      
      # Install dependencies
      echo "Installing Python dependencies..."
      pip install -r "${local.source_dir}/requirements.txt" \
        --target "$BUILD_DIR/package" \
        --platform manylinux2014_x86_64 \
        --implementation cp \
        --python-version 3.13 \
        --only-binary=:all: \
        --quiet 2>/dev/null || \
      pip install -r "${local.source_dir}/requirements.txt" \
        --target "$BUILD_DIR/package" \
        --quiet
      
      # Copy agent source
      echo "Copying agent source..."
      cp "${local.source_dir}/agent.py" "$BUILD_DIR/package/"
      
      # Create zip file - exclude __pycache__ directories to avoid Python version incompatibility
      echo "Creating zip archive..."
      (cd "$BUILD_DIR/package" && zip -r -q "../${var.artifact_name}.zip" . -x '*__pycache__*' -x '*.pyc')
      
      # Verify zip was created
      ls -la "$BUILD_DIR/${var.artifact_name}.zip"
      
      # Upload to S3
      echo "Uploading to s3://${var.s3_bucket}/${local.s3_key}..."
      aws s3 cp "$BUILD_DIR/${var.artifact_name}.zip" \
        "s3://${var.s3_bucket}/${local.s3_key}" \
        --region ${data.aws_region.current.region}
      
      echo "=== Agent artifact built and uploaded ==="
      echo "S3 URI: s3://${var.s3_bucket}/${local.s3_key}"
    EOF
  }
}

# -----------------------------------------------------------------------------
# Verify Upload (data source to confirm S3 object exists)
# -----------------------------------------------------------------------------

data "aws_s3_object" "artifact" {
  depends_on = [null_resource.build_agent]
  
  bucket = var.s3_bucket
  key    = local.s3_key
}
