# Generate trading signals (generate-signals.py) into data.json.
# Usage: powershell -File tools\windows\signals.ps1
. (Join-Path $PSScriptRoot "_config.ps1")
Load-LocalEnv

$code = Start-LoggedPython -Script "generate-signals.py" -PyArgs @() -LogName "signals"
if ($code -ne 0) { Write-Host "signals FAILED exit=$code" -ForegroundColor Red; exit $code }
Write-Host "signals OK"
