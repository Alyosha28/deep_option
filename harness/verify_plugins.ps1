# GOAI plugin family validator (DSH orchestration layer, plugin split Phase 3)
# Usage:
#   powershell -ExecutionPolicy Bypass -File harness\verify_plugins.ps1
# Checks:
#   1. config JSON parses and every enabled plugin references an existing file
#   2. every plugins\*.host.js passes node --check (syntax)
#   3. every plugin file is a Cordis plugin: returns an object with name/apply
#   4. base mode plugins are enabled by default sanity
# NOTE: ASCII-only on purpose (PowerShell 5.1 UTF-8 pitfall, project pitfall #2).
# NOTE: requires node.exe on PATH.

$ErrorActionPreference = 'Stop'
$harness = $PSScriptRoot
$configPath = Join-Path $harness 'config\goai.plugins.json'
$pluginDir = Join-Path $harness 'plugins'
$failed = $false

Write-Output '==== GOAI plugin family verification ===='

# 0) node present?
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Output '[FAIL] node.exe not found on PATH (needed for syntax checks)'
    exit 1
}

# 1) config JSON
if (-not (Test-Path $configPath)) {
    Write-Output "[FAIL] missing config: $configPath"
    exit 1
}
$config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
Write-Output "[OK] config parses: $configPath (version $($config.version))"
Write-Output "[INFO] baseMode = $($config.baseMode -join ', ')"

$enabled = @()
foreach ($entry in $config.plugins.PSObject.Properties) {
    $name = $entry.Name
    $meta = $entry.Value
    $file = Join-Path $harness $meta.file
    if (-not (Test-Path $file)) {
        Write-Output "[FAIL] plugin $name references missing file: $($meta.file)"
        $failed = $true
        continue
    }
    $state = if ($meta.enabled) { 'enabled ' } else { 'disabled' }
    Write-Output "[INFO] $name : $state : $($meta.tools -join ', ') -> $($meta.file)"
    if ($meta.enabled) { $enabled += $name }
}

# 2) syntax check every plugin
Get-ChildItem $pluginDir -Filter '*.host.js' | ForEach-Object {
    $result = & node --check $_.FullName 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Output "[OK] syntax: $($_.Name)"
    } else {
        Write-Output "[FAIL] syntax: $($_.Name)"
        Write-Output $result
        $failed = $true
    }
}

# 3) Cordis shape: last non-comment line should be the closing of 'return { ... }'
Get-ChildItem $pluginDir -Filter '*.host.js' | ForEach-Object {
    $text = Get-Content $_.FullName -Raw -Encoding UTF8
    if ($text -notmatch 'return\s*\{' -or $text -notmatch "name:\s*'goai-") {
        Write-Output "[FAIL] shape: $($_.Name) is not a Cordis plugin object (missing return { name: 'goai-...' })"
        $failed = $true
    }
}

# 4) base mode sanity: all baseMode plugins must be enabled
foreach ($name in $config.baseMode) {
    $meta = $config.plugins.$name
    if (-not $meta) {
        Write-Output "[FAIL] baseMode references unknown plugin: $name"
        $failed = $true
    } elseif (-not $meta.enabled) {
        Write-Output "[FAIL] baseMode plugin $name is disabled in config"
        $failed = $true
    }
}
if (-not $failed) {
    Write-Output '[OK] base mode intact'
}

if ($failed) {
    Write-Output '==== VERIFICATION FAILED ===='
    exit 1
}
Write-Output "==== VERIFICATION PASSED (enabled: $($enabled -join ', ')) ===="
