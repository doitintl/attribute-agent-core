# =============================================================================
# Agent Runtime Module Outputs
# =============================================================================

output "agent_runtime_arn" {
  description = "ARN of the agent runtime"
  value       = data.external.agent_runtime_info.result.arn
}

output "agent_runtime_id" {
  description = "ID of the agent runtime"
  value       = data.external.agent_runtime_info.result.id
}

output "agent_runtime_name" {
  description = "Name of the agent runtime"
  value       = local.runtime_name
}

output "agent_runtime_status" {
  description = "Status of the agent runtime"
  value       = data.external.agent_runtime_info.result.status
}

output "agent_runtime_version" {
  description = "Version of the agent runtime"
  value       = data.external.agent_runtime_info.result.version
}
