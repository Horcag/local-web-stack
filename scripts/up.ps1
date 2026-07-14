param(
  [switch]$NoMcp
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "Starting local web stack services (Valkey, SearXNG, Crawl4AI) via Docker Compose..."
docker compose up -d --build --remove-orphans
if ($LASTEXITCODE -ne 0) {
  throw "docker compose up failed"
}

Write-Host "Syncing host Python dependencies for MCP server..."
uv sync

Write-Host "Waiting for Crawl4AI container to be ready..."
for ($i = 0; $i -lt 15; $i++) {
  try {
    $health = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:11235/health" -TimeoutSec 2 -ErrorAction SilentlyContinue
    if ($health -and $health.status -eq "healthy") {
      Write-Host "Crawl4AI is ready!"
      break
    }
  } catch {}
  Start-Sleep -Seconds 1
}

& "$PSScriptRoot\verify.ps1"

if (-not $NoMcp) {
  & "$PSScriptRoot\start-mcp-http.ps1"
}
