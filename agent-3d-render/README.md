# 3D Rendering Agent

Multi-tenant 3D rendering agent for Amazon Bedrock AgentCore. Tenants describe scenes in plain language and receive rendered images.

## How It Works

1. Tenant sends a prompt: *"A red sports car in a showroom with dramatic lighting"*
2. Claude interprets the prompt and generates a structured scene graph
3. Blender Cycles renders the scene using GPU ray tracing
4. Result is uploaded to S3, scoped by tenant ID

## Files

| File | Purpose |
|------|---------|
| `agent.py` | AgentCore entrypoint — handles requests, calls Bedrock, orchestrates rendering |
| `blender_runtime.py` | Scene generation and Blender rendering logic |
| `requirements.txt` | Python dependencies |
| `fixtures/` | Test scene JSON files for local development |
| `hdri_assets/` | HDRI environment maps for lighting |
| `scripts/` | Local testing and debugging tools |

## Local Testing

```bash
# Install Blender 4.2.0 (one-time)
mkdir -p .blender && cd .blender
curl -LO https://download.blender.org/release/Blender4.2/blender-4.2.0-macos-arm64.dmg
# ... mount and copy Blender.app

# Render a test fixture
scripts/render_local.py fixtures/regr_skyscraper.json --quality fast --check
```

## Deployment

Deployed via Terraform from the parent directory. See the root `README.md` for instructions.
