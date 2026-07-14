$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $root 'work\local-web-mcp-http.pid'

if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Output 'local-web MCP: no pid file'
    return
}

$rootPid = [int] ((Get-Content -LiteralPath $pidFile -TotalCount 1).Trim())
$all = Get-CimInstance Win32_Process
$rootProcess = $all | Where-Object { $_.ProcessId -eq $rootPid }
if (-not $rootProcess) {
    Remove-Item -LiteralPath $pidFile -Force
    Write-Output 'local-web MCP: stale pid file removed'
    return
}

$commandLine = [string] $rootProcess.CommandLine
foreach ($needle in @('powershell', 'run-mcp-http.ps1')) {
    if ($commandLine -notlike "*$needle*") {
        throw "local-web MCP PID $rootPid command line did not contain expected marker: $needle"
    }
}

$children = @()
$queue = @($rootPid)
while ($queue.Count -gt 0) {
    $current = $queue[0]
    $queue = @($queue | Select-Object -Skip 1)
    $directChildren = @($all | Where-Object { $_.ParentProcessId -eq $current })
    foreach ($child in $directChildren) {
        $children += $child
        $queue += $child.ProcessId
    }
}

$targets = @($rootProcess) + $children
[array]::Reverse($targets)
foreach ($target in $targets) {
    Stop-Process -Id $target.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Output "local-web MCP: stopped PID $($target.ProcessId) $($target.Name)"
}

Remove-Item -LiteralPath $pidFile -Force
