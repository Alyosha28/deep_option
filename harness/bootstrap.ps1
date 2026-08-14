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

# 4) optional: start the standalone engine
if ($StartEngine) {
    if (-not $listener) {
        Write-Output "==== starting standalone engine (http://127.0.0.1:$Port/) ===="
        & $python -m src.ui_server --port $Port
    } else {
        Write-Output "engine already running; open http://127.0.0.1:$Port/ for the four-panel terminal"
    }
    exit 0
}

# 5) DSH registration guide
Write-Output ''
Write-Output '==== DSH registration guide (hand these steps to the session assistant) ===='
Write-Output '1. read harness\plugins\goai-bridge.host.js and pass its content as code.host to cordis_define (new plugin, idPrefix: goai)'
Write-Output '2. on a new machine: ensure the DSH session process sees GOAI_PROJECT_ROOT=<repo> (or the plugin falls back to its default path)'
Write-Output '3. cordis_run to activate (host-only, no approval needed)'
Write-Output '4. say "goai_state" in the session to verify: the plugin auto-starts the engine and returns the decision-card summary'
Write-Output 'standalone mode (judge machine): python -m src.ui_server --port 8000'
