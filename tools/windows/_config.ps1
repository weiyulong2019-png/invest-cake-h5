# Shared config for local maintenance scripts (Windows).
# Repo root = two levels up from tools\windows\
# decode child-process stdout as UTF-8 (PowerShell 5.1 defaults to GBK -> mojibake logs)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$VenvPython = "C:\Users\win\.workbuddy\binaries\python\envs\invest-cake\Scripts\python.exe"
$LogDir = Join-Path $RepoRoot "logs"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

function Load-LocalEnv {
    $envFile = Join-Path $RepoRoot ".env.local"
    if (Test-Path $envFile) {
        # ReadAllLines + explicit UTF8: Get-Content mangles UTF-8/LF files on PS 5.1
        [System.IO.File]::ReadAllLines($envFile, [System.Text.Encoding]::UTF8) | ForEach-Object {
            $line = $_.Trim()
            if ($line -eq "" -or $line.StartsWith("#")) { return }
            $i = $line.IndexOf("=")
            if ($i -lt 1) { return }
            $k = $line.Substring(0, $i).Trim()
            $v = $line.Substring($i + 1).Trim()
            Set-Item -Path "Env:$k" -Value $v
        }
    }
}

function Start-LoggedPython {
    param(
        [Parameter(Mandatory = $true)][string]$Script,
        [string[]]$PyArgs = @(),
        [Parameter(Mandatory = $true)][string]$LogName
    )
    Set-Location $RepoRoot
    # force UTF-8 IO: scripts print emoji, PowerShell pipe defaults to GBK
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONUTF8 = "1"
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $log = Join-Path $LogDir "$LogName-$stamp.log"
    $ErrorActionPreference = "Continue"
    & $VenvPython $Script @PyArgs *>&1 | Out-File -FilePath $log -Encoding UTF8
    $code = $LASTEXITCODE
    Write-Host "log: $log"
    return $code
}
