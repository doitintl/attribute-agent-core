"""
Weather SaaS Agent for AgentCore Runtime Instances.

A multi-tenant weather assistant that uses Strands Agents SDK and Claude Haiku 4.5.
Designed to run on AgentCore capacity provider instances.

Custom header support:
- Reads x-tenant-id from request headers (requires requestHeaderAllowlist config)
- Falls back to tenant_id in payload body for backward compatibility
"""

import json
import logging
import os
from datetime import datetime

from bedrock_agentcore import BedrockAgentCoreApp, RequestContext
from strands import Agent, tool
from strands.models import BedrockModel
from strands_tools import http_request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("weather-saas-agent")

# Initialize the AgentCore app
app = BedrockAgentCoreApp(debug=True)

# Configuration
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

# Header name for tenant ID (must be in requestHeaderAllowlist)
TENANT_ID_HEADER = "x-tenant-id"

# Well-known US cities for weather lookups
US_CITIES = {
    "seattle": ("47.6062", "-122.3321"),
    "new york": ("40.7128", "-74.0060"),
    "los angeles": ("34.0522", "-118.2437"),
    "chicago": ("41.8781", "-87.6298"),
    "houston": ("29.7604", "-95.3698"),
    "phoenix": ("33.4484", "-112.0740"),
    "philadelphia": ("39.9526", "-75.1652"),
    "san antonio": ("29.4241", "-98.4936"),
    "san diego": ("32.7157", "-117.1611"),
    "dallas": ("32.7767", "-96.7970"),
    "san jose": ("37.3382", "-121.8863"),
    "austin": ("30.2672", "-97.7431"),
    "jacksonville": ("30.3322", "-81.6557"),
    "san francisco": ("37.7749", "-122.4194"),
    "columbus": ("39.9612", "-82.9988"),
    "indianapolis": ("39.7684", "-86.1581"),
    "denver": ("39.7392", "-104.9903"),
    "boston": ("42.3601", "-71.0589"),
    "miami": ("25.7617", "-80.1918"),
    "atlanta": ("33.7490", "-84.3880"),
    "portland": ("45.5152", "-122.6784"),
    "las vegas": ("36.1699", "-115.1398"),
    "detroit": ("42.3314", "-83.0458"),
    "minneapolis": ("44.9778", "-93.2650"),
    "charlotte": ("35.2271", "-80.8431"),
    "nashville": ("36.1627", "-86.7816"),
    "baltimore": ("39.2904", "-76.6122"),
    "milwaukee": ("43.0389", "-87.9065"),
    "pittsburgh": ("40.4406", "-79.9959"),
    "cleveland": ("41.4993", "-81.6944"),
    "fort worth": ("32.7555", "-97.3308"),
}

SYSTEM_PROMPT = """You are a weather assistant for a SaaS product.

You can:
1. Look up forecasts for well-known US cities via the get_weather_forecast tool.
2. Fall back to http_request against https://api.weather.gov for anything the tool doesn't cover.

When displaying responses:
- Format weather data in a human-readable way
- Highlight important information like temperature, precipitation, and alerts
- Handle errors gracefully
- Be concise but informative
"""

# Initialize the model
model = BedrockModel(model_id=BEDROCK_MODEL_ID, region_name=AWS_REGION)


@tool
def get_weather_forecast(city: str) -> str:
    """
    Get weather forecast for a US city.
    
    Args:
        city: Name of the city (e.g., "Seattle", "New York")
    
    Returns:
        Weather forecast information or error message
    """
    import urllib.request
    import urllib.error
    
    city_lower = city.lower().strip()
    
    if city_lower not in US_CITIES:
        return f"City '{city}' not in our database. Known cities: {', '.join(sorted(US_CITIES.keys()))}"
    
    lat, lon = US_CITIES[city_lower]
    
    try:
        # Get the forecast URL from weather.gov
        points_url = f"https://api.weather.gov/points/{lat},{lon}"
        req = urllib.request.Request(points_url, headers={"User-Agent": "weather-saas-agent/1.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            points_data = json.loads(response.read().decode())
        
        forecast_url = points_data["properties"]["forecast"]
        
        # Get the actual forecast
        req = urllib.request.Request(forecast_url, headers={"User-Agent": "weather-saas-agent/1.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            forecast_data = json.loads(response.read().decode())
        
        periods = forecast_data["properties"]["periods"][:4]  # Next 4 periods
        
        result = f"Weather forecast for {city.title()}:\n\n"
        for period in periods:
            result += f"**{period['name']}**: {period['temperature']}°{period['temperatureUnit']}\n"
            result += f"  {period['shortForecast']}\n"
            result += f"  Wind: {period['windSpeed']} {period['windDirection']}\n\n"
        
        return result
        
    except urllib.error.URLError as e:
        return f"Error fetching weather for {city}: {e}"
    except Exception as e:
        return f"Unexpected error: {e}"


def build_agent(tenant_id: str = None) -> Agent:
    """Build a weather agent, optionally customized for a tenant."""
    
    if tenant_id:
        system_prompt = f"{SYSTEM_PROMPT}\n\nYou are answering on behalf of tenant: {tenant_id}"
    else:
        system_prompt = SYSTEM_PROMPT
    
    return Agent(
        model=model,
        system_prompt=system_prompt,
        tools=[get_weather_forecast, http_request],
    )


def get_tenant_id_from_headers(headers: dict) -> str:
    """
    Extract tenant_id from request headers.
    Headers are case-insensitive, so check common variations.
    """
    if not headers:
        return None
    
    # Try exact header name first
    if TENANT_ID_HEADER in headers:
        return headers[TENANT_ID_HEADER]
    
    # Try lowercase version (headers may be normalized)
    lower_headers = {k.lower(): v for k, v in headers.items()}
    header_lower = TENANT_ID_HEADER.lower()
    
    if header_lower in lower_headers:
        return lower_headers[header_lower]
    
    # Also check x-tenant-id without the hyphen variations
    for key in ["x-tenant-id", "x_tenant_id", "tenant-id", "tenant_id"]:
        if key in lower_headers:
            return lower_headers[key]
    
    return None


@app.entrypoint
def invoke(payload: dict, context: RequestContext):
    """
    AgentCore entrypoint for the weather-saas agent.
    
    Tenant ID resolution order:
    1. x-tenant-id header (preferred - requires requestHeaderAllowlist)
    2. tenant_id in payload body (fallback for backward compatibility)
    
    Expected payload format:
    {
        "prompt": "What's the weather in Seattle?"
    }
    
    With header:
    x-tenant-id: acme
    """
    logger.info(f"Received payload: {json.dumps(payload)}")
    
    # Get request headers from context
    request_headers = context.request_headers if context else {}
    logger.info(f"Request headers: {json.dumps(request_headers)}")
    
    prompt = payload.get("prompt") or payload.get("task") or payload.get("message")
    session_id = payload.get("session_id", "unknown")
    
    # Get tenant_id: prefer header, fall back to payload body
    tenant_id = get_tenant_id_from_headers(request_headers)
    if not tenant_id:
        tenant_id = payload.get("tenant_id") or payload.get("x_tenant_id")
    
    logger.info(f"Resolved tenant_id: {tenant_id} (from_header={tenant_id in (request_headers or {}).values()})")
    
    if not prompt:
        return {
            "error": "No prompt provided",
            "usage": "Send {'prompt': 'What is the weather in Seattle?'} with x-tenant-id header"
        }
    
    logger.info(f"Processing prompt for tenant={tenant_id}, session={session_id}")
    
    try:
        agent = build_agent(tenant_id)
        response = agent(prompt)
        content = str(response)
        
        # Extract usage metrics if available
        usage = {}
        metrics = getattr(response, "metrics", None)
        if metrics:
            try:
                totals = metrics.accumulated_usage
                usage = {
                    "input_tokens": totals.get("inputTokens", 0),
                    "output_tokens": totals.get("outputTokens", 0)
                }
            except Exception:
                pass
        
        logger.info(f"Response generated: {len(content)} chars, usage={usage}")
        
        return {
            "agent": "weather-saas",
            "tenant_id": tenant_id,
            "session_id": session_id,
            "model": BEDROCK_MODEL_ID,
            "response": content,
            "usage": usage,
            "timestamp": datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        logger.exception(f"Error processing prompt: {e}")
        return {
            "error": str(e),
            "tenant_id": tenant_id,
            "session_id": session_id
        }


# For local testing
if __name__ == "__main__":
    app.run()
