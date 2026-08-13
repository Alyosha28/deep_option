# Scheduled-task entry point for the GOAI macro source watcher.
# Keeps this file ASCII-only so PowerShell 5.1 reads it correctly without a BOM.
param(
    [string]$Config = "",
    [string]$Library = "",
    [int]$MaxItems = 20,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Config) { $Config = Join-Path $root "data\sources_config.json" }
if (-not $Library) { $Library = Join-Path $root "data\policy_events" }

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "venv python not found: $python"
}

$logDir = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "watch_scheduled.log"
$errFile = Join-Path $logDir "watch_scheduled.err.log"

$arguments = @(
    "-m", "src.macro_source_watcher",
    "--run-once",
    "--config", $Config,
    "--library", $Library,
    "--max-items", $MaxItems
)
if ($DryRun) { $arguments += "--dry-run" }

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

$timestamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
Add-Content -LiteralPath $logFile -Value "=== run start $timestamp ===" -Encoding utf8

$process = Start-Process -FilePath $python -ArgumentList $arguments `
    -WorkingDirectory $root -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput $logFile -RedirectStandardError $errFile
$exitCode = $process.ExitCode

Add-Content -LiteralPath $logFile -Value "=== run end, exit code $exitCode ===" -Encoding utf8
exit $exitCode
