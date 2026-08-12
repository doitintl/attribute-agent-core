# =============================================================================
# Agent Artifact Module - Outputs
# =============================================================================

output "s3_bucket" {
  description = "S3 bucket containing the agent artifact"
  value       = var.s3_bucket
}

output "s3_key" {
  description = "S3 key of the agent artifact"
  value       = local.s3_key
}

output "s3_uri" {
  description = "Full S3 URI of the agent artifact"
  value       = "s3://${var.s3_bucket}/${local.s3_key}"
}

output "artifact_hash" {
  description = "Hash of the agent source files (for change detection)"
  value       = local.agent_source_hash
}

output "etag" {
  description = "ETag of the uploaded S3 object"
  value       = data.aws_s3_object.artifact.etag
}
