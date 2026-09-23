# Register the Windows scheduled task that replaces the macOS launchd job.
# Triggers every 5 minutes, Monday-Friday, all day; scheduled-refresh.ps1 decides what runs.
# Run once, as the current user (no admin needed for a user-scoped task).
param(
    [string]$TaskName = "InvestCake-Refresh",
    [switch]$Uninstall
)

$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$script = Join-Path $PSScriptRoot "scheduled-refresh.ps1"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "uninstalled $TaskName"
    exit 0
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 00:00
$trigger.Repetition.Interval = "PT5M"
$trigger.Repetition.Duration = "P1D"
# critical: do not let Task Scheduler kill the task when idle state ends
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "InvestCake H5 market data refresh (5 min, weekdays)" -Force | Out-Null
Write-Host "installed $TaskName -> $script"
Write-Host "check: Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
