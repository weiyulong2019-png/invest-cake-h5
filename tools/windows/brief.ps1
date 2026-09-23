# Refresh daily_brief.json (post-close brief), with the same guard as refresh-brief.sh:
# only commit/push when date == today, available == true and items are non-empty.
# Usage: powershell -File tools\windows\brief.ps1 [-Publish]
param([switch]$Publish)

. (Join-Path $PSScriptRoot "_config.ps1")
Load-LocalEnv

$code = Start-LoggedPython -Script "daily-brief.py" -PyArgs @() -LogName "brief"
if ($code -ne 0) { Write-Host "brief FAILED exit=$code" -ForegroundColor Red; exit $code }

$jsonPath = Join-Path $RepoRoot "daily_brief.json"
$raw = Get-Content $jsonPath -Raw -Encoding UTF8
$d = $raw | ConvertFrom-Json
$today = (Get-Date).ToString("yyyy-MM-dd")
$ok = ($d.date -eq $today) -and ($d.available -eq $true) -and (@($d.items).Count -gt 0)

if (-not $ok) {
    Write-Host "guard FAILED: daily_brief.json is not today/empty - abort" -ForegroundColor Yellow
    exit 1
}
Write-Host "brief OK ($today, $(@($d.items).Count) items)"

if ($Publish) {
    Set-Location $RepoRoot
    & git add daily_brief.json
    & git diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        & git commit -m "daily_brief refresh $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
        & git push origin main
    }
}
