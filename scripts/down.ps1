$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$mcpPidFile = "$root\work\local-web-mcp-http.pid"
if (Test-Path $mcpPidFile) {
  $mcpPidText = (Get-Content $mcpPidFile -Raw).Trim()
  if ($mcpPidText) {
    $mcpPid = [int]$mcpPidText
    $procInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$mcpPid" -ErrorAction SilentlyContinue
    if ($procInfo -and $procInfo.CommandLine -like "*run-mcp-http.ps1*") {
      Stop-Process -Id $mcpPid
      Write-Host "Stopped local-web MCP wrapper PID $mcpPid"
    }
  }
  Remove-Item -LiteralPath $mcpPidFile -Force
}

$pidFile = "$root\work\crawl4ai-api.pid"
if (Test-Path $pidFile) {
  $pidText = (Get-Content $pidFile -Raw).Trim()
  if ($pidText) {
    $stopPid = [int]$pidText
    $procInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$stopPid" -ErrorAction SilentlyContinue
    if ($procInfo -and $procInfo.CommandLine -like "*service.crawl4ai_api:app*") {
      Stop-Process -Id $stopPid
      Write-Host "Stopped lingering local Crawl4AI API PID $stopPid"
    }
  }
  Remove-Item -LiteralPath $pidFile -Force
}

docker compose down
