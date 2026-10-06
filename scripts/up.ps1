param([switch]$NoMcp)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$composeArgs = @("compose", "up", "-d", "--build")
if ($NoMcp) { $composeArgs += @("--scale", "mcp=0") }
& docker @composeArgs
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
  try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:11235/health" -TimeoutSec 2
    if ($health.status -eq "healthy") { $ready = $true; break }
  } catch {}
  Start-Sleep -Seconds 1
}
if (-not $ready) { throw "Crawl4AI did not become ready" }
& "$PSScriptRoot\verify.ps1"
Write-Host "Research dashboard: http://127.0.0.1:11235/"
if (-not $NoMcp) { Write-Host "Streamable HTTP MCP: http://127.0.0.1:8765/mcp" }
