$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$workDir = Join-Path $root "work"
$pidFile = Join-Path $workDir "local-web-mcp-http.pid"
$outLog = Join-Path $workDir "local-web-mcp-http.out.log"
$errLog = Join-Path $workDir "local-web-mcp-http.err.log"
$port = if ($env:LOCAL_WEB_MCP_PORT) { [int]$env:LOCAL_WEB_MCP_PORT } else { 8765 }
$endpoint = "http://127.0.0.1:$port/mcp"
$commonScript = Join-Path $env:USERPROFILE 'bin\mcp-stack-common.ps1'
if (Test-Path -LiteralPath $commonScript) {
  . $commonScript
} else {
  # Standalone fallback for GitHub portability
  function Test-McpInitialize {
      param(
          [string]$Url,
          [int]$TimeoutSeconds = 10
      )
      $body = @{
          jsonrpc = '2.0'
          id = 1
          method = 'initialize'
          params = @{
              protocolVersion = '2025-06-18'
              capabilities = @{}
              clientInfo = @{
                  name = 'mcp-stack-health'
                  version = '1'
              }
          }
      } | ConvertTo-Json -Depth 8 -Compress
      $sessionId = $null
      try {
          $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSeconds -Method Post -ContentType 'application/json' -Headers @{
              Accept = 'application/json, text/event-stream'
          } -Body $body
          $sessionId = [string] (@($response.Headers['Mcp-Session-Id'])[0])
          $content = [string] $response.Content
          $ok = $response.StatusCode -ge 200 -and $response.StatusCode -lt 300 -and $content -match '"result"'
          return $ok
      } catch {
          return $false
      } finally {
          if ($sessionId) {
              try {
                  Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5 -Method Delete -Headers @{
                      'Mcp-Session-Id' = $sessionId
                  } | Out-Null
              } catch {}
          }
      }
  }

  function Wait-McpInitialize {
      param(
          [string]$Url,
          [int]$TimeoutSeconds = 45
      )
      $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
      while ((Get-Date) -lt $deadline) {
          if (Test-McpInitialize -Url $Url -TimeoutSeconds 5) {
              return $true
          }
          Start-Sleep -Milliseconds 500
      }
      return $false
  }
}

New-Item -ItemType Directory -Force -Path $workDir | Out-Null

$listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if (Test-McpInitialize -Url $endpoint) {
  Write-Host "local-web MCP already healthy on $endpoint"
  return
}

if (Test-Path -LiteralPath $pidFile) {
  & (Join-Path $PSScriptRoot 'stop-mcp-http.ps1') | Out-Null
  $listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $port -State Listen -ErrorAction SilentlyContinue
}

if ($listener) {
  $listenerPids = ($listener | Select-Object -ExpandProperty OwningProcess -Unique) -join ', '
  throw "local-web MCP port $port belongs to unverified PID(s): $listenerPids"
}

$runner = Join-Path $PSScriptRoot "run-mcp-http.ps1"
$process = Start-Process `
  -FilePath "powershell.exe" `
  -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $runner) `
  -WorkingDirectory $root `
  -RedirectStandardOutput $outLog `
  -RedirectStandardError $errLog `
  -WindowStyle Hidden `
  -PassThru

Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ASCII
Write-Host "Started local-web MCP wrapper PID $($process.Id); logs: $outLog / $errLog"

if (Wait-McpInitialize -Url $endpoint -TimeoutSeconds 90) {
  Write-Host "local-web MCP ready on $endpoint"
  return
}

& (Join-Path $PSScriptRoot 'stop-mcp-http.ps1') | Out-Null
throw "local-web MCP did not complete MCP initialize on $endpoint; see $errLog"
