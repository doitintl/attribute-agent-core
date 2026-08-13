# Attribute Agent Core

Terraform-based infrastructure for deploying **DoiT Attribute EC2 sensor** on **Amazon Bedrock AgentCore Runtime Instances**.

This module automatically installs the Attribute sensor on AgentCore-managed EC2 instances using EventBridge + Lambda + SSM, enabling cost observability for AI agent workloads.

## Background

Amazon Bedrock AgentCore Runtime Instances (launched August 2026) provides persistent EC2 infrastructure for AI agents. When an agent session starts, AgentCore launches EC2 instances in your account that can persist for up to 14 days. This creates an opportunity to install the DoiT Attribute sensor on these instances to gain visibility into agent compute costs.

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

The included `agent/` directory contains a **Weather SaaS** demo application that simulates a multi-tenant AI agent. It:

- Responds to weather queries for various US cities using Claude (via Amazon Bedrock)
- Accepts an `x-tenant-id` HTTP header to identify the calling tenant
- The Attribute sensor captures this header and uses it to attribute compute and AI costs per tenant

This demonstrates how to build a multi-tenant AI application where costs can be broken down by customer using DoiT Attribute.

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   AgentCore     │     │   EventBridge   │     │     Lambda      │
│                 │     │                 │     │                 │
│  Launches EC2   │────▶│  EC2 State      │────▶│  Installs       │
│  Instance       │     │  Change Event   │     │  Sensor via SSM │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                                                        │
                                                        ▼
                                               ┌─────────────────┐
                                               │   EC2 Instance  │
                                               │                 │
                                               │  ┌───────────┐  │
                                               │  │  zprobe   │  │
                                               │  │  (sensor) │  │
                                               │  └───────────┘  │
                                               │  ┌───────────┐  │
                                               │  │  Agent    │  │
                                               │  │  Workload │  │
                                               │  └───────────┘  │
                                               └─────────────────┘
```

## Why This Approach?

Traditional sensor deployment methods don't work with AgentCore:

| Method | Why It Doesn't Work |
|--------|---------------------|
| **Custom AMI** | AgentCore only accepts `operatingSystem` parameter, not custom AMI IDs |
| **ASG Lifecycle Hooks** | AgentCore ASGs are operator-protected; adding hooks fails |
| **Launch Template UserData** | AgentCore manages launch templates internally |

The EventBridge + Lambda + SSM approach works because:
- EventBridge receives EC2 state-change events regardless of launch source
- Lambda can filter for AgentCore-specific tags
- SSM Run Command works on any instance with the SSM agent installed

## Prerequisites

1. **Terraform** >= 1.5.0
2. **AWS CLI** >= 2.36 (for AgentCore commands)
3. **DoiT Attribute token** from the Attribute Dashboard
4. **S3 bucket** for agent code artifacts
5. **jq** installed locally (for Terraform external data sources)
6. **EC2 Managed Resource Visibility** must be enabled:
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
sensor_token = "eyJhbGciOiJFZERTQSJ9..."
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

By default, Terraform automatically builds the agent code from the `agent/` directory, packages it with dependencies, and uploads it to S3. To use a pre-existing artifact instead, set `build_agent_artifact = false` and specify `agent_s3_key`.

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
| `allowed_instance_types` | Allowed EC2 types | `["c6i.large", "c6a.large", "c7i.large"]` |
| `ebs_volume_size` | EBS volume size in GB | `30` |

### Agent Runtime

| Variable | Description | Default |
|----------|-------------|---------|
| `agent_s3_bucket` | S3 bucket for agent code | **Required** |
| `agent_s3_key` | S3 key to agent zip | `weather-saas-agent/weather-saas-agent.zip` |
| `python_runtime` | Python version | `PYTHON_3_13` |
| `request_header_allowlist` | Custom headers to pass to agent | `["x-tenant-id"]` |

### Attribute Sensor

| Variable | Description | Default |
|----------|-------------|---------|
| `sensor_token` | DoiT Attribute token | **Required** |
| `sensor_workload_name` | Workload name in Attribute | `agentcore-agent` |
| `sensor_memory_limit` | Sensor memory limit (bytes) | `524288000` (500MB) |

## Directory Structure

```
attribute-agent-core/
├── README.md                    # This file
├── agent/
│   ├── agent.py                 # Weather SaaS agent (example)
│   └── requirements.txt         # Python dependencies
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

## Custom Header Support

This deployment supports custom headers for multi-tenant applications. The `x-tenant-id` header is allowed by default.

### Agent Code

```python
from bedrock_agentcore import BedrockAgentCoreApp, RequestContext

@app.entrypoint
def invoke(payload: dict, context: RequestContext):
    # Headers arrive lowercase
    tenant_id = context.request_headers.get('x-tenant-id')
```

### Client Code (boto3)

```python
import threading
import boto3

_thread_local = threading.local()

def add_tenant_header(request, **kwargs):
    tenant_id = getattr(_thread_local, 'tenant_id', None)
    if tenant_id:
        request.headers.add_header('x-tenant-id', tenant_id)

client = boto3.client("bedrock-agentcore", region_name="us-east-1")
client.meta.events.register('before-sign.bedrock-agentcore.InvokeAgentRuntime', add_tenant_header)

# Set tenant before invoking
_thread_local.tenant_id = "acme"
response = client.invoke_agent_runtime(...)
```

## Load Testing

Use the included load generator to test your deployment:

```bash
# Install dependencies
pip install boto3

# Set environment variables (optional)
export AGENT_RUNTIME_ARN="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/my-runtime"
export RATE_MULTIPLIER=0.2

# Run for 30 minutes
timeout 1800 python scripts/loadgen-agentcore.py
```

The load generator simulates 10 tenants with different rate limits:
- Enterprise: 60 req/min (acme-core, globex-core, stark-core)
- Business: 30 req/min (umbrella-core, wayne-core, soylent-core, vandelay-core)
- Free: 5 req/min (initech-core, hooli-core, piedpiper-core)

## Outputs

After deployment, Terraform outputs useful values:

```bash
# Get the agent runtime ARN for invocation
terraform output agent_runtime_arn

# Get the invoke command
terraform output invoke_command
```

## Troubleshooting

### Sensor Not Installing

1. **Check Lambda logs**:
   ```bash
   aws logs tail /aws/lambda/attribute-agentcore-sensor-installer --since 10m
   ```

2. **Verify instance has AgentCore tag**:
   ```bash
   aws ec2 describe-tags --filters "Name=resource-id,Values=i-xxx" \
     --query 'Tags[?Key==`bedrock-agentcore:capacity-provider-id`]'
   ```

3. **Check SSM agent status**:
   ```bash
   aws ssm describe-instance-information --filters "Key=InstanceIds,Values=i-xxx"
   ```

### Sensor Crash-Looping

Check the zprobe binary flags. The sensor uses single-dash flags:

| Wrong | Correct |
|-------|---------|
| `--token` | `-bearer-token` |
| `--max-memory` | `-memlimit` |
| `--workload` | `-workload-name` |

### Download Timeout

Fresh EC2 instances may have slow IPv6. The Lambda uses `wget -4` to force IPv4.

## Cost

| Component | Cost |
|-----------|------|
| Lambda | ~$0.001 per 1000 instance launches |
| EventBridge | Free (AWS service events) |
| Secrets Manager | $0.40/month per secret |
| SSM Run Command | Free |
| EC2 instances | Standard EC2 pricing |

## Cleanup

```bash
# Destroy all resources
terraform destroy
```

**Note**: AgentCore resources (capacity provider, runtime) are destroyed via AWS CLI in the Terraform destroy provisioners.

## References

- [Amazon Bedrock AgentCore Documentation](https://docs.aws.amazon.com/bedrock-agentcore/)
- [AgentCore Runtime Instances - How it Works](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-how-it-works.html)
- [AWS Blog: Runtime Instances](https://aws.amazon.com/blogs/aws/runtime-instances-persistent-compute-for-production-ai-agents-on-amazon-bedrock-agentcore/)
- [DoiT Attribute EC2 Sensor Installation](https://help.doit.com/docs/attribute/integrations/cloud-integrations/aws/attribute-ec2-sensor-installation)
- [AgentCore Request Header Allowlist](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-header-allowlist.html)
