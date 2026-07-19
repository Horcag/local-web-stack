$ErrorActionPreference = "Stop"

$searxng = "http://127.0.0.1:8088"
$crawl4ai = "http://127.0.0.1:11235"
$searxngHeaders = @{ "X-Real-IP" = "127.0.0.1" }

Write-Host "Checking SearXNG JSON API..."
$search = Invoke-RestMethod -Method Get -Uri "$searxng/search?q=example&format=json" -Headers $searxngHeaders -TimeoutSec 30
$count = @($search.results).Count
Write-Host "SearXNG OK: $count results"

Write-Host "Checking Crawl4AI health..."
$health = Invoke-RestMethod -Method Get -Uri "$crawl4ai/health" -TimeoutSec 30
Write-Host "Crawl4AI health: $($health | ConvertTo-Json -Compress)"

Write-Host "Checking Crawl4AI markdown extraction..."
$body = @{ url = "https://example.com" } | ConvertTo-Json -Compress
$markdown = Invoke-RestMethod -Method Post -Uri "$crawl4ai/md" -ContentType "application/json" -Body $body -TimeoutSec 90
$preview = ($markdown.markdown -replace "\s+", " ").Trim()
if ($preview.Length -gt 160) {
  $preview = $preview.Substring(0, 160)
}
Write-Host "Crawl4AI markdown OK: $preview"

Write-Host "Local web stack verified."
