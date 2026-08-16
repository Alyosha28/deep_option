# GOAI demo environment bootstrap (DSH orchestration layer, Phase 1c)
# Usage:
#   powershell -ExecutionPolicy Bypass -File harness\bootstrap.ps1              # self-check + print DSH registration guide
#   powershell -ExecutionPolicy Bypass -File harness\bootstrap.ps1 -StartEngine # self-check, then start standalone engine
# NOTE: this script never injects anything into DSH. Plugin registration must be done
#       by the DSH session assistant via cordis_define/cordis_run. This script only
#       guarantees the standalone path: "judge machine without DSH still runs the demo".
# NOTE: ASCII-only on purpose. Windows PowerShell 5.1 misreads UTF-8 scripts without BOM
#       (project pitfall #2); keep every message in plain English to stay immune.

param(
    [switch]$StartEngine,
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Write-Output '==== GOAI environment self-check ===='

# 0) DSH bridge project root (cross-machine parameterization)
Write-Output "[INFO] repo root: $repo"
if ($env:GOAI_PROJECT_ROOT) {
    Write-Output "[OK] GOAI_PROJECT_ROOT=$env:GOAI_PROJECT_ROOT (DSH plugin will use it)"
} else {
    Write-Output '[WARN] GOAI_PROJECT_ROOT is not set; the DSH plugin falls back to its default path.'
    Write-Output "       On a new machine set it for the DSH session process: `$env:GOAI_PROJECT_ROOT = '$repo'"
}

# 1) Python (prefer project venv)
$venvPy = Join-Path $repo '.venv\Scripts\python.exe'
$python = $null
if (Test-Path $venvPy) {
    $python = $venvPy
    Write-Output "[OK] venv python: $venvPy"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = 'python'
    Write-Output '[WARN] .venv not found, falling back to PATH python (futu-api deps may be missing)'
} else {
    Write-Output '[FAIL] no Python interpreter found; run: python -m venv .venv then pip install -r requirements.txt'
    exit 1
}

# 2) curl.exe (used by the DSH plugin HTTP bridge; standalone mode does not need it)
if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
    Write-Output '[OK] curl.exe available'
} else {
    Write-Output '[WARN] curl.exe not found (needed only by the DSH plugin bridge)'
}

# 3) port status
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    Write-Output "[INFO] port $Port already has a listener (the DSH plugin will reuse that engine)"
} else {
    Write-Output "[INFO] port $Port is free"
}

# 4) agent preset sync (goai-options template <-> installed instance)
$verifyPreset = Join-Path $PSScriptRoot 'verify_preset.ps1'
if (Test-Path $verifyPreset) {
    Write-Output '[INFO] checking goai-options preset (template vs installed)...'
    & $verifyPreset
    $presetCheckOk = $?
    if ($presetCheckOk) {
        Write-Output '[OK] goai-options preset verified'
    } else {
        Write-Output '[WARN] goai-options preset drift or missing install detected; run: powershell -ExecutionPolicy Bypass -File harness\verify_preset.ps1 -Sync'
    }
} else {
    Write-Output '[WARN] harness\verify_preset.ps1 missing (preset checker not present)'
}

# 5) optional: start the standalone engine
if ($StartEngine) {
    if (-not $listener) {
        Write-Output "==== starting standalone engine (http://127.0.0.1:$Port/) ===="
        & $python -m src.ui_server --port $Port
    } else {
        Write-Output "engine already running; open http://127.0.0.1:$Port/ for the four-panel terminal"
    }
    exit 0
}

# 6) plugin family config (user selects which modules to load)
$configPath = Join-Path $PSScriptRoot 'config\goai.plugins.json'
$enabled = @()
$disabled = @()
$legacy = $false
if (Test-Path $configPath) {
    $cfg = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($entry in $cfg.plugins.PSObject.Properties) {
        if ($entry.Value.enabled) { $enabled += $entry.Name } else { $disabled += $entry.Name }
    }
    Write-Output "[INFO] plugin config loaded: $configPath"
    Write-Output "[INFO] base mode: $($cfg.baseMode -join ', ')"
} else {
    Write-Output "[WARN] plugin config not found: $configPath (falling back to legacy single goai-bridge plugin)"
    $legacy = $true
}

# 7) DSH registration guide (config-driven; hand these steps to the session assistant)
Write-Output ''
Write-Output '==== DSH registration guide (hand these steps to the session assistant) ===='
Write-Output '0. register plugins from a session with cordis_define/cordis_run available (use the cordis preset, or a development preset). The goai-options product preset ships with tool-cordis disabled by design.'
Write-Output '1. on a new machine: ensure the DSH session process sees GOAI_PROJECT_ROOT=<repo> (or plugins fall back to their default path)'
if ($legacy) {
    Write-Output '2. read harness\plugins\goai-bridge.host.js and pass its content as code.host to cordis_define (new plugin, idPrefix: goai)'
    Write-Output '3. cordis_run to activate (host-only, no approval needed)'
} else {
    Write-Output '2. for EACH enabled plugin below: read its file and pass the content as code.host to cordis_define (new plugin, idPrefix: goai), then cordis_run (host-only, no approval needed):'
    foreach ($name in $enabled) {
        Write-Output "   - ${name}: harness\plugins\${name}.host.js"
    }
    Write-Output '3. do NOT register the legacy goai-bridge.host.js alongside this family (tool names overlap)'
    if ($disabled.Count -gt 0) {
        Write-Output "4. disabled plugins (not registered): $($disabled -join ', ') - edit harness\config\goai.plugins.json to change"
    }
}
Write-Output '5. say "goai_state" in the session to verify: the plugin auto-starts the engine and returns the decision-card summary'
Write-Output 'standalone mode (judge machine): python -m src.ui_server --port 8000'
