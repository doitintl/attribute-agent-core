"""
Lambda function to automatically install DoiT Attribute EC2 sensor on AgentCore instances.

Triggered by EventBridge when an EC2 instance is launched with AgentCore tags.
Uses SSM Run Command to execute the sensor installation script.
"""

import json
import logging
import os
import time
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Configuration
WORKLOAD_NAME = os.environ.get("WORKLOAD_NAME", "agentcore-agent")
MEMORY_LIMIT = os.environ.get("MEMORY_LIMIT", "524288000")  # 500 MiB
SECRET_NAME = os.environ.get("SECRET_NAME", "attribute/sensor-token")
MAX_WAIT_SECONDS = int(os.environ.get("MAX_WAIT_SECONDS", "300"))

ssm_client = boto3.client("ssm")
secrets_client = boto3.client("secretsmanager")
ec2_client = boto3.client("ec2")


def get_attribute_token() -> str:
    """Retrieve Attribute token from Secrets Manager."""
    try:
        response = secrets_client.get_secret_value(SecretId=SECRET_NAME)
        secret = json.loads(response["SecretString"])
        return secret.get("token", "")
    except ClientError as e:
        logger.error(f"Failed to retrieve secret {SECRET_NAME}: {e}")
        raise


def is_agentcore_instance(instance_id: str) -> bool:
    """
    Check if the instance was launched by AgentCore.
    
    Uses the observed tag keys from actual AgentCore-launched instances:
    - bedrock-agentcore:capacity-provider-id (primary identifier)
    - aws:ec2:managed-launch = agentcore-runtime-instance (alternative)
    """
    try:
        response = ec2_client.describe_tags(
            Filters=[
                {"Name": "resource-id", "Values": [instance_id]},
                {"Name": "key", "Values": ["bedrock-agentcore:capacity-provider-id"]}
            ]
        )
        return len(response.get("Tags", [])) > 0
    except ClientError as e:
        logger.warning(f"Failed to check tags for {instance_id}: {e}")
        return False


def get_agentcore_metadata(instance_id: str) -> dict:
    """
    Get AgentCore metadata from instance tags.
    
    Returns dict with:
    - capacity_provider_id: The capacity provider that launched this instance
    - runtime_session_id: The runtime session ID
    - asg_name: The Auto Scaling group name
    """
    try:
        response = ec2_client.describe_tags(
            Filters=[
                {"Name": "resource-id", "Values": [instance_id]}
            ]
        )
        metadata = {}
        for tag in response.get("Tags", []):
            key = tag.get("Key", "")
            value = tag.get("Value", "")
            if key == "bedrock-agentcore:capacity-provider-id":
                metadata["capacity_provider_id"] = value
            elif key == "bedrock-agentcore:runtime-session-id":
                metadata["runtime_session_id"] = value
            elif key == "aws:autoscaling:groupName":
                metadata["asg_name"] = value
        return metadata
    except ClientError as e:
        logger.warning(f"Failed to get metadata for {instance_id}: {e}")
        return {}


def wait_for_ssm_agent(instance_id: str, timeout: int = 120) -> bool:
    """Wait for SSM agent to be online on the instance."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = ssm_client.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
            )
            if response.get("InstanceInformationList"):
                info = response["InstanceInformationList"][0]
                if info.get("PingStatus") == "Online":
                    logger.info(f"SSM agent online on {instance_id}")
                    return True
        except ClientError as e:
            logger.debug(f"SSM agent not ready: {e}")
        time.sleep(10)
    return False


def install_sensor(instance_id: str, token: str) -> dict:
    """
    Install Attribute sensor on the instance using SSM Run Command.
    
    Uses the official standalone-install.sh script:
    sudo ./standalone-install.sh WORKLOAD_NAME MEMORY_LIMIT TOKEN
    """
    install_commands = [
        "#!/bin/bash",
        "set -e",
        "",
        "# ===== TIMING PROOF: Check if AgentCore agent is running =====",
        'echo "=== TIMING CHECK START ==="',
        'echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"',
        'echo "Uptime: $(uptime -p)"',
        "",
        "# Check for any AgentCore/agent-related processes",
        "AGENT_PROCS=$(ps aux | grep -E '(agentcore|agent-runtime|nimbus|uvicorn)' | grep -v grep || true)",
        'if [ -n "$AGENT_PROCS" ]; then',
        '    echo "AGENT STATUS: RUNNING"',
        "else",
        '    echo "AGENT STATUS: NOT YET STARTED"',
        "fi",
        'echo "=== TIMING CHECK END ==="',
        "",
        "# Check if sensor is already installed and running",
        "if systemctl is-active --quiet zprobe 2>/dev/null; then",
        '    echo "Attribute sensor already running"',
        "    exit 0",
        "fi",
        "",
        "# Download and run the standalone installer (use IPv4 to avoid slow IPv6)",
        'echo "Starting sensor installation at $(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"',
        "cd /tmp",
        "wget -4 -q https://storage.googleapis.com/attrb-artifacts/standalone-install.sh",
        "chmod +x ./standalone-install.sh",
        f"sudo ./standalone-install.sh {WORKLOAD_NAME} {MEMORY_LIMIT} {token}",
        "",
        "# Start the service (standalone-install.sh enables but doesn't start it)",
        "sudo systemctl start zprobe",
        "",
        "# Verify installation",
        "sleep 5",
        'echo ""',
        'echo "=== POST-INSTALL STATUS ==="',
        'echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"',
        "",
        "if systemctl is-active --quiet zprobe; then",
        '    echo "Attribute sensor installed and running successfully"',
        "    journalctl -u zprobe -n 5 --no-pager",
        "else",
        '    echo "ERROR: Sensor failed to start"',
        "    systemctl status zprobe --no-pager",
        "    journalctl -u zprobe -n 20 --no-pager",
        "    exit 1",
        "fi",
    ]
    
    try:
        response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={
                "commands": install_commands,
                "executionTimeout": ["600"]
            },
            TimeoutSeconds=MAX_WAIT_SECONDS,
            Comment=f"Install DoiT Attribute sensor on AgentCore instance {instance_id}"
        )
        
        command_id = response["Command"]["CommandId"]
        logger.info(f"Started SSM command {command_id} on {instance_id}")
        
        return {
            "command_id": command_id,
            "instance_id": instance_id,
            "status": "started"
        }
        
    except ClientError as e:
        logger.error(f"Failed to send SSM command to {instance_id}: {e}")
        raise


def handler(event: dict, context) -> dict:
    """
    Lambda handler for EventBridge EC2 instance state-change events.
    
    Expected event format (from EventBridge):
    {
        "source": "aws.ec2",
        "detail-type": "EC2 Instance State-change Notification",
        "detail": {
            "instance-id": "i-1234567890abcdef0",
            "state": "running"
        }
    }
    """
    logger.info(f"Received event: {json.dumps(event)}")
    
    # Extract instance ID from EventBridge event
    detail = event.get("detail", {})
    instance_id = detail.get("instance-id")
    state = detail.get("state")
    
    if not instance_id:
        logger.error("No instance-id in event")
        return {"status": "error", "message": "Missing instance-id"}
    
    if state != "running":
        logger.info(f"Instance {instance_id} state is {state}, skipping")
        return {"status": "skipped", "reason": f"State is {state}, not running"}
    
    # Check if this is an AgentCore instance
    if not is_agentcore_instance(instance_id):
        logger.info(f"Instance {instance_id} is not an AgentCore instance, skipping")
        return {"status": "skipped", "reason": "Not an AgentCore instance"}
    
    # Get AgentCore metadata for logging
    metadata = get_agentcore_metadata(instance_id)
    logger.info(f"Processing AgentCore instance {instance_id}: {metadata}")
    
    # Wait for SSM agent to be available
    if not wait_for_ssm_agent(instance_id):
        logger.error(f"SSM agent not available on {instance_id}")
        return {"status": "error", "message": "SSM agent timeout"}
    
    # Get Attribute token from Secrets Manager
    try:
        token = get_attribute_token()
    except Exception as e:
        return {"status": "error", "message": f"Failed to get token: {e}"}
    
    # Install the sensor
    try:
        result = install_sensor(instance_id, token)
        logger.info(f"Sensor installation initiated: {result}")
        return {"status": "success", **result}
    except Exception as e:
        logger.error(f"Sensor installation failed: {e}")
        return {"status": "error", "message": str(e)}


# For local testing
if __name__ == "__main__":
    test_event = {
        "source": "aws.ec2",
        "detail-type": "EC2 Instance State-change Notification",
        "detail": {
            "instance-id": "i-0123456789abcdef0",
            "state": "running"
        }
    }
    print(handler(test_event, None))
