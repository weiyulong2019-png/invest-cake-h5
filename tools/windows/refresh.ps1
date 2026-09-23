# Refresh market data (data.json).
# Usage:
#   powershell -File tools\windows\refresh.ps1                # auto mode (sina + akshare)
#   powershell -File tools\windows\refresh.ps1 --manual       # needs MX_APIKEY
#   powershell -File tools\windows\refresh.ps1 --skip-a       # HK only window
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$PyArgs)

. (Join-Path $PSScriptRoot "_config.ps1")
Load-LocalEnv

$code = Start-LoggedPython -Script "refresh-data.py" -PyArgs $PyArgs -LogName "refresh"
if ($code -ne 0) { Write-Host "refresh FAILED exit=$code" -ForegroundColor Red; exit $code }

# refresh watchlist quotes too (same as auto-refresh.sh)
$code2 = Start-LoggedPython -Script "manage-watchlist.py" -PyArgs @("refresh") -LogName "watchlist"
if ($code2 -ne 0) { Write-Host "watchlist refresh FAILED exit=$code2" -ForegroundColor Red; exit $code2 }

Write-Host "refresh OK"
