$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $env:LOCAL_WEB_MCP_TRANSPORT) { $env:LOCAL_WEB_MCP_TRANSPORT = "streamable-http" }
if (-not $env:LOCAL_WEB_MCP_HOST) { $env:LOCAL_WEB_MCP_HOST = "127.0.0.1" }
if (-not $env:LOCAL_WEB_MCP_PORT) { $env:LOCAL_WEB_MCP_PORT = "8765" }
if (-not $env:LOCAL_WEB_MCP_PATH) { $env:LOCAL_WEB_MCP_PATH = "/mcp" }
if (-not $env:SEARXNG_URL) { $env:SEARXNG_URL = "http://127.0.0.1:8088" }
if (-not $env:CRAWL4AI_URL) { $env:CRAWL4AI_URL = "http://127.0.0.1:11235" }

Write-Host "Starting local-web MCP on http://$($env:LOCAL_WEB_MCP_HOST):$($env:LOCAL_WEB_MCP_PORT)$($env:LOCAL_WEB_MCP_PATH)"
uv run .\mcp\local_web_mcp.py
