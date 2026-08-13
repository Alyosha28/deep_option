# Register or remove the Windows Scheduled Task for the GOAI macro source watcher.
# ASCII-only on purpose (PowerShell 5.1 + no-BOM UTF-8 safety).
#
# Install example:
#   powershell -ExecutionPolicy Bypass -File scripts\install_watcher_task.ps1 -Minutes 60
# Remove:
#   powershell -ExecutionPolicy Bypass -File scripts\install_watcher_task.ps1 -Remove
#
# Limitation: the task uses an Interactive logon principal, so it runs while the
# current user is logged on. Unattended boot scheduling would require storing
# credentials, which this script intentionally does not do.
param(
    [int]$Minutes = 60,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$taskName = "GOAI-PolicyWatcher"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed scheduled task: $taskName"
    exit 0
}

if ($Minutes -lt 5) {
    throw "Minutes must be at least 5."
}

$runner = Join-Path $PSScriptRoot "run_watcher_scheduled.ps1"
if (-not (Test-Path -LiteralPath $runner)) {
    throw "runner script not found: $runner"
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
    "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`""
)
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(2)) `
    -RepetitionInterval (New-TimeSpan -Minutes $Minutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Description "GOAI macro source watcher (run-once polling)" `
    -Force | Out-Null

Write-Output "Registered scheduled task: $taskName (every $Minutes minutes)"
Write-Output "Runner: $runner"
