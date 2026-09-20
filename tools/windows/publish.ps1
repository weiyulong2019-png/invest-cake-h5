# Publish data updates to GitHub -> Cloudflare Pages auto-deploys from main.
# Guard: runs validate-public-data.py first; never force-pushes.
# Usage: powershell -File tools\windows\publish.ps1 [-Message "msg"]
param([string]$Message = "")

. (Join-Path $PSScriptRoot "_config.ps1")
Load-LocalEnv
Set-Location $RepoRoot

# github.com 本机直连会超时；用系统代理，但只在本次进程注入（不写进 git config，
# 否则代理关掉后 git 会静默挂住）。
$reg = Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" -ErrorAction SilentlyContinue
if ($reg -and $reg.ProxyEnable -eq 1 -and $reg.ProxyServer) {
    $env:http_proxy = $reg.ProxyServer
    $env:https_proxy = $reg.ProxyServer
    Write-Host "git via system proxy: $($reg.ProxyServer)"
}

Write-Host "validating public data..."
& $VenvPython "validate-public-data.py" --scope market
if ($LASTEXITCODE -ne 0) {
    Write-Host "validation FAILED - abort publish" -ForegroundColor Red
    exit 1
}

& git add data.json watchlist.json
& git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host "no staged change - nothing to publish"
    exit 0
}

if ($Message -eq "") { $Message = "market data update $(Get-Date -Format 'yyyy-MM-dd HH:mm')" }
& git commit -m $Message
if ($LASTEXITCODE -ne 0) { Write-Host "commit failed" -ForegroundColor Red; exit 1 }

& git push origin main
if ($LASTEXITCODE -ne 0) {
    Write-Host "push failed - run once interactively to complete GitHub auth (GCM)" -ForegroundColor Yellow
    exit 1
}
Write-Host "published -> Cloudflare Pages will deploy shortly"
