#!/usr/bin/env python3
"""
Load generator for AgentCore Runtime Instances.

Generates continuous traffic to the weather-saas agent deployed on AgentCore,
similar to the K8s load generator but adapted for the AgentCore invoke API.

Custom header support:
- Sends tenant_id as x-tenant-id header (requires requestHeaderAllowlist config)
- Uses boto3 event handlers to inject custom headers

Usage:
    export AWS_REGION=us-east-1
    python loadgen-agentcore.py

Environment variables:
    AGENT_RUNTIME_ARN: ARN of the agent runtime (required)
    RATE_MULTIPLIER: Scale factor for request rate (default: 0.2)
    SESSION_ID: Runtime session ID - use same ID to keep instance alive (default: auto-generated)
"""

import asyncio
import json
import logging
import os
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("agentcore-loadgen")

# Configuration
AGENT_RUNTIME_ARN = os.environ.get(
    "AGENT_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-east-1:058264544288:runtime/weatherSaasAgent-kHby5SE5ju"
)
RATE_MULTIPLIER = float(os.environ.get("RATE_MULTIPLIER", "0.2"))
SESSION_ID = os.environ.get("SESSION_ID", f"loadgen-session-{uuid.uuid4().hex}")
REGION = os.environ.get("AWS_REGION", "us-east-1")

# Custom header name for tenant ID
TENANT_ID_HEADER = "x-tenant-id"

# US cities for weather queries
US_CITIES = [
    "New York", "Los Angeles", "Chicago", "Houston", "Phoenix",
    "Philadelphia", "San Antonio", "San Diego", "Dallas", "San Jose",
    "Austin", "Jacksonville", "Fort Worth", "Columbus", "Charlotte",
    "Seattle", "Denver", "Boston", "Nashville", "Portland"
]

# Tenant configuration (matching the K8s deployment)
TENANTS = {
    "acme-core": {"rate_limit_per_min": 60, "plan": "enterprise"},
    "globex-core": {"rate_limit_per_min": 60, "plan": "enterprise"},
    "initech-core": {"rate_limit_per_min": 5, "plan": "free"},
    "umbrella-core": {"rate_limit_per_min": 30, "plan": "business"},
    "stark-core": {"rate_limit_per_min": 60, "plan": "enterprise"},
    "wayne-core": {"rate_limit_per_min": 30, "plan": "business"},
    "soylent-core": {"rate_limit_per_min": 30, "plan": "business"},
    "hooli-core": {"rate_limit_per_min": 5, "plan": "free"},
    "piedpiper-core": {"rate_limit_per_min": 5, "plan": "free"},
    "vandelay-core": {"rate_limit_per_min": 30, "plan": "business"},
}

# Configure boto3 client with longer timeouts for AgentCore
config = Config(
    read_timeout=120,
    connect_timeout=30,
    retries={'max_attempts': 2}
)

# Thread-local storage for tenant ID to pass to event handler
_thread_local = threading.local()


def add_tenant_header(request, **kwargs):
    """
    Boto3 event handler to add x-tenant-id custom header.
    Called before request signing (before-sign event).
    """
    tenant_id = getattr(_thread_local, 'tenant_id', None)
    if tenant_id:
        request.headers.add_header(TENANT_ID_HEADER, tenant_id)


# Create client and register event handler
client = boto3.client("bedrock-agentcore", region_name=REGION, config=config)
event_system = client.meta.events
event_system.register('before-sign.bedrock-agentcore.InvokeAgentRuntime', add_tenant_header)


def invoke_agent(tenant_id: str, city: str) -> dict:
    """
    Invoke the AgentCore agent with a weather query.
    
    Sends tenant_id as x-tenant-id header instead of payload body.
    """
    prompt = f"What is the weather in {city}?"
    
    # Payload only contains the prompt - tenant_id goes in header
    payload = json.dumps({
        "prompt": prompt
    })
    
    # Set tenant_id in thread-local storage for the event handler
    _thread_local.tenant_id = tenant_id
    
    start = time.monotonic()
    try:
        # Use raw bytes for payload (not base64)
        response = client.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            qualifier="DEFAULT",
            runtimeSessionId=SESSION_ID,
            payload=payload.encode('utf-8'),  # Raw bytes, not base64
            contentType="application/json"
        )
        
        latency = time.monotonic() - start
        
        # Read streaming response body
        response_body = response.get("response")
        if hasattr(response_body, "read"):
            response_body = response_body.read()
        elif response_body is None:
            response_body = b""
        
        return {
            "success": True,
            "tenant_id": tenant_id,
            "city": city,
            "latency": latency,
            "status": response.get("statusCode", 200),
            "response_length": len(response_body) if response_body else 0
        }
        
    except Exception as e:
        latency = time.monotonic() - start
        return {
            "success": False,
            "tenant_id": tenant_id,
            "city": city,
            "latency": latency,
            "error": str(e)
        }
    finally:
        # Clear thread-local storage
        _thread_local.tenant_id = None


async def tenant_loop(executor: ThreadPoolExecutor, tenant_id: str, rate_per_min: int):
    """Generate load for a single tenant at the specified rate."""
    scaled_rate = max(rate_per_min * RATE_MULTIPLIER, 0.1)
    interval = 60.0 / scaled_rate
    
    loop = asyncio.get_event_loop()
    
    while True:
        city = random.choice(US_CITIES)
        
        # Run the blocking boto3 call in a thread pool
        result = await loop.run_in_executor(executor, invoke_agent, tenant_id, city)
        
        if result["success"]:
            logger.info(
                "tenant=%s status=%s latency=%.2fs city=%s response_len=%d",
                result["tenant_id"],
                result["status"],
                result["latency"],
                result["city"],
                result["response_length"]
            )
        else:
            logger.warning(
                "tenant=%s request_failed latency=%.2fs city=%s error=%s",
                result["tenant_id"],
                result["latency"],
                result["city"],
                result["error"]
            )
        
        # Add jitter to the interval
        await asyncio.sleep(interval * random.uniform(0.5, 1.5))


async def main():
    """Main entry point - starts load generation for all tenants."""
    logger.info("=" * 60)
    logger.info("AgentCore Load Generator")
    logger.info("=" * 60)
    logger.info(f"Agent Runtime ARN: {AGENT_RUNTIME_ARN}")
    logger.info(f"Session ID: {SESSION_ID}")
    logger.info(f"Rate Multiplier: {RATE_MULTIPLIER}")
    logger.info(f"Tenants: {len(TENANTS)}")
    logger.info(f"Custom Header: {TENANT_ID_HEADER}")
    logger.info("=" * 60)
    
    # Create a thread pool for blocking boto3 calls
    executor = ThreadPoolExecutor(max_workers=20)
    
    try:
        tasks = [
            tenant_loop(executor, tenant_id, config["rate_limit_per_min"])
            for tenant_id, config in TENANTS.items()
        ]
        logger.info(f"Starting load generator for {len(tasks)} tenants...")
        await asyncio.gather(*tasks)
    finally:
        executor.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
