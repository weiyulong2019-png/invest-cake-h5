# Windows port of auto-refresh.sh.
# Intended to be triggered every 5 minutes on weekdays by Task Scheduler;
# this script decides what to actually run based on local time.
#
# Schedule (mirrors the macOS launchd version):
#   05:55-06:10   pre-market signals
#   09:25         call auction signals + quotes
#   09:30-11:30   quotes every tick; signals at :00/:30
#   11:31-12:59   HK only (A-share lunch break)
#   13:00-15:15   A-share quotes + signals every 30 min
#   15:16-16:15   HK only
#   16:15         final HK close
param([switch]$NoPublish)

. (Join-Path $PSScriptRoot "_config.ps1")
Load-LocalEnv
Set-Location $RepoRoot

$now = Get-Date
$dow = [int]$now.DayOfWeek   # 0=Sunday
if ($dow -eq 0 -or $dow -eq 6) { Write-Host "weekend - skip"; exit 0 }

$hhmm = $now.ToString("HHmm")
$lock = Join-Path $env:TEMP "invest-cake-refresh.lock"
if (Test-Path $lock) {
    $age = (Get-Date) - (Get-Item $lock).LastWriteTime
    if ($age.TotalMinutes -lt 4) { Write-Host "previous run still active - skip"; exit 0 }
}
New-Item -ItemType File -Path $lock -Force | Out-Null
try {
    # ---- 05:55-06:10 pre-market ----
    if ($hhmm -ge "0555" -and $hhmm -le "0610") {
        $dayLock = Join-Path $env:TEMP "invest-cake-premarket-$($now.ToString('yyyyMMdd')).lock"
        if (Test-Path $dayLock) { Write-Host "pre-market already done today - skip"; exit 0 }
        $c = Start-LoggedPython -Script "generate-signals.py" -LogName "signals"
        if ($c -ne 0) { Write-Host "signals failed" -ForegroundColor Red; exit $c }
        New-Item -ItemType File -Path $dayLock -Force | Out-Null
        if (-not $NoPublish) { & (Join-Path $PSScriptRoot "publish.ps1") -Message "pre-market signals" }
        exit 0
    }

    if ($hhmm -lt "0555") { Write-Host "off hours - skip"; exit 0 }
    if ($hhmm -gt "0610" -and $hhmm -lt "0925") { Write-Host "off hours - skip"; exit 0 }
    if ($hhmm -gt "1615") { Write-Host "after close - skip"; exit 0 }

    $runSignal = @("0925", "1000", "1030", "1100", "1130", "1300", "1330", "1400", "1430", "1500", "1515", "1615") -contains $hhmm
    $skipA = ($hhmm -gt "1515" -and $hhmm -le "1615") -or ($hhmm -gt "1130" -and $hhmm -lt "1300")

    if ($skipA) {
        $c = Start-LoggedPython -Script "refresh-data.py" -PyArgs @("--skip-a") -LogName "refresh"
    } else {
        $c = Start-LoggedPython -Script "refresh-data.py" -LogName "refresh"
    }
    if ($c -ne 0) { Write-Host "quote refresh failed" -ForegroundColor Red; exit $c }

    $c2 = Start-LoggedPython -Script "manage-watchlist.py" -PyArgs @("refresh") -LogName "watchlist"
    if ($c2 -ne 0) { Write-Host "watchlist refresh failed" -ForegroundColor Yellow }

    if ($runSignal) {
        $c3 = Start-LoggedPython -Script "generate-signals.py" -LogName "signals"
        if ($c3 -ne 0) { Write-Host "signals failed" -ForegroundColor Yellow }
    }

    if (-not $NoPublish) {
        $msg = if ($runSignal) { "market data + signals $(Get-Date -Format 'HH:mm')" } else { "market data $(Get-Date -Format 'HH:mm')" }
        & (Join-Path $PSScriptRoot "publish.ps1") -Message $msg
    }
}
finally {
    Remove-Item $lock -Force -ErrorAction SilentlyContinue
}
