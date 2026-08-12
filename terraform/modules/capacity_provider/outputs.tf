# =============================================================================
# Capacity Provider Module Outputs
# =============================================================================

output "capacity_provider_arn" {
  description = "ARN of the capacity provider"
  value       = data.external.capacity_provider_info.result.arn
}

output "capacity_provider_id" {
  description = "ID of the capacity provider"
  value       = data.external.capacity_provider_info.result.id
}

output "capacity_provider_name" {
  description = "Name of the capacity provider"
  value       = local.capacity_provider_name
}

output "capacity_provider_status" {
  description = "Status of the capacity provider"
  value       = data.external.capacity_provider_info.result.status
}
