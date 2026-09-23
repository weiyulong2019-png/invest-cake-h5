# Local preview: static server for the H5 dashboard.
# Usage: powershell -File tools\windows\serve.ps1 [-Port 8080]
param([int]$Port = 8080)

. (Join-Path $PSScriptRoot "_config.ps1")
Set-Location $RepoRoot
Write-Host "serving $RepoRoot at http://127.0.0.1:$Port"
& $VenvPython -m http.server $Port --bind 127.0.0.1
