# Attribute Agent Core

Terraform-based infrastructure for deploying **DoiT Attribute EC2 sensor** on **Amazon Bedrock AgentCore Runtime Instances**.

This module automatically installs the Attribute sensor on AgentCore-managed EC2 instances using EventBridge + Lambda + SSM, enabling cost observability for AI agent workloads.


### How AgentCore Runtime Instances Work

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Amazon Bedrock AgentCore                         │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌─────────────────────┐     ┌──────────────────────────────────┐  │
│  │   Capacity Provider │────▶│  EC2 Instance (your account)     │  │
│  ├─────────────────────┤     ├──────────────────────────────────┤  │
│  │ • OS: Linux x86/ARM │     │ • Agent Runtime (container/zip)  │  │
│  │ • Instance types    │     │ • Shared session storage         │  │
│  │ • VPC/Subnets/SGs  │     │ • Up to 14 days lifetime         │  │
│  │ • EBS volumes       │     │ • Managed by AgentCore           │  │
│  │ • IAM permissions   │     └──────────────────────────────────┘  │
│  └─────────────────────┘                                            │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### Attribute Sensor Requirements

| Requirement | Details |
|-------------|---------|
| Kernel version | 5.10 or newer |
| Architecture | `x86_64` or `arm64` |
| Privileges | `sudo` access (handled by SSM) |
| Token | Per-account installation token from Attribute Dashboard |

## Demo Agent

The included `agent-3d-render/` directory contains a **3D Rendering SaaS** demo application that simulates a multi-tenant AI agent. It:

- Takes plain-language scene descriptions and produces photorealistic renders using Blender Cycles with NVIDIA OptiX GPU ray tracing
- Uses Claude (via Amazon Bedrock) to interpret prompts and generate structured scene graphs
- Accepts an `x-tenant-id` HTTP header to identify the calling tenant
- The Attribute sensor captures this header and uses it to attribute compute and AI costs per tenant

This demonstrates how to build a multi-tenant AI application where costs can be broken down by customer using DoiT Attribute.

## Prerequisites

1. **Terraform** >= 1.5.0
2. **AWS CLI** >= 2.36 (for AgentCore commands)
3. **DoiT Attribute token** from the Attribute Dashboard
4. **S3 bucket** for agent code artifacts
5. **jq** installed locally (for Terraform external data sources)
6. **GPU instance capacity** in your AWS account (g5 family)
7. **EC2 Managed Resource Visibility** must be enabled:
   ```bash
   aws ec2 modify-managed-resource-visibility --region us-west-2 --default-visibility visible
   ```
   This allows you to see AgentCore-managed EC2 instances in the AWS Console and via CLI. Without this, the instances are hidden by default.

## Quick Start

### 1. Clone and Configure

```bash
cd attribute-agent-core/terraform

# Copy the template
cp terraform.tfvars.template terraform.tfvars

# Edit with your values
vim terraform.tfvars
```

### 2. Required Variables

At minimum, you need to set:

```hcl
# AWS Region
aws_region = "us-east-1"

# S3 bucket for agent code (must already exist)
agent_s3_bucket = "your-artifacts-bucket"

# DoiT Attribute sensor token (from Attribute Dashboard > Setup > API Tokens)
sensor_token = "234333..."

# Use GPU instances for 3D rendering
allowed_instance_types = ["g5.xlarge", "g5.2xlarge", "g5.4xlarge"]

# Larger EBS for Blender + models
ebs_volume_size = 100
```

### 3. Deploy

```bash
# Initialize Terraform
terraform init

# Review the plan
terraform plan

# Deploy (this will also build and upload the agent code)
terraform apply
```

By default, Terraform automatically builds the agent code from the `agent-3d-render/` directory, packages it with dependencies, and uploads it to S3. To use a pre-existing artifact instead, set `build_agent_artifact = false` and specify `agent_s3_key`.

## Configuration Variables

### General

| Variable | Description | Default |
|----------|-------------|---------|
| `aws_region` | AWS region for deployment | `us-east-1` |
| `name_prefix` | Prefix for all resource names | `attribute-agentcore` |

### Network

| Variable | Description | Default |
|----------|-------------|---------|
| `vpc_id` | VPC ID (empty = default VPC) | `""` |
| `subnet_ids` | Subnet IDs (empty = auto-discover) | `[]` |
| `security_group_ids` | Security group IDs (empty = default SG) | `[]` |

### Capacity Provider

| Variable | Description | Default |
|----------|-------------|---------|
| `operating_system` | OS for instances | `LINUX_X86_64` |
| `allowed_instance_types` | Allowed EC2 types | `["g5.xlarge", "g5.2xlarge", "g5.4xlarge"]` |
| `ebs_volume_size` | EBS volume size in GB | `100` |

### Agent Runtime

| Variable | Description | Default |
|----------|-------------|---------|
| `agent_s3_bucket` | S3 bucket for agent code | **Required** |
| `agent_s3_key` | S3 key to agent zip | `3d-render-agent/3d-render-agent.zip` |
| `python_runtime` | Python version | `PYTHON_3_13` |
| `request_header_allowlist` | Custom headers to pass to agent | `["x-tenant-id"]` |
| `session_idle_timeout` | Idle timeout in seconds | `3600` (1 hour) |
| `session_max_duration` | Max session duration in seconds | `86400` (24 hours) |

### Attribute Sensor

| Variable | Description | Default |
|----------|-------------|---------|
| `sensor_token` | DoiT Attribute token | **Required** |
| `sensor_workload_name` | Workload name in Attribute | `3d-render-agent` |
| `sensor_memory_limit` | Sensor memory limit (bytes) | `524288000` (500MB) |

## Directory Structure

```
attribute-agent-core/
├── README.md                    # This file
├── agent-3d-render/
│   ├── agent.py                 # 3D render agent entrypoint
│   ├── blender_runtime.py       # Blender scene generation & rendering
│   ├── requirements.txt         # Python dependencies
│   ├── Dockerfile               # Container build (optional)
│   ├── fixtures/                # Test scene fixtures
│   │   ├── *.json               # Scene graph test cases
│   │   └── _prod_baseline/      # Reference renders
│   ├── hdri_assets/             # HDRI environment maps
│   └── scripts/
│       ├── render_local.py      # Local testing without AWS
│       ├── tenant_test.py       # Multi-tenant test harness
│       └── capability_probe.py  # Blender version compatibility
├── scripts/
│   └── loadgen-agentcore.py     # Load generator for testing
├── lambda/
│   └── index.py                 # Sensor installer Lambda
└── terraform/
    ├── main.tf                  # Main configuration
    ├── variables.tf             # Input variables
    ├── outputs.tf               # Output values
    ├── terraform.tfvars.template # Template for variables
    └── modules/
        ├── iam/                 # IAM roles and policies
        ├── secrets/             # Secrets Manager
        ├── lambda/              # Sensor installer Lambda
        ├── eventbridge/         # EC2 state-change rule
        ├── capacity_provider/   # AgentCore capacity provider
        └── agent_runtime/       # AgentCore runtime
```
