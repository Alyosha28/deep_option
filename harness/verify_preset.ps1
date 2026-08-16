# GOAI preset template verifier / installer (DSH agent preset)
# Usage:
#   powershell -ExecutionPolicy Bypass -File harness\verify_preset.ps1
#   powershell -ExecutionPolicy Bypass -File harness\verify_preset.ps1 -Sync
# Checks (template):
#   1. harness\preset\agent.cordis.yml + preset.yml exist
#   2. preset.yml has non-empty name + description (goai_* ladder + fallback)
#   3. agent.cordis.yml keeps product-safety defaults:
#        - tool-cordis row disabled (host cordisInspect registry collision fix)
#        - delegation group disabled (smaller tool surface)
#        - persona / tool-vision rows present
#   4. template skills\ dir mirrors installed preset (recursive byte compare)
#   5. installed preset at $DSH_HOME/.agent-presets/goai-options (or default
#      home) matches template byte-for-byte; -Sync copies template there first.
# NOTE: ASCII-only output on purpose (project pitfall #2, PowerShell 5.1 BOM).

param([switch]$Sync)

$ErrorActionPreference = 'Stop'
$harness = $PSScriptRoot
$preset = Join-Path $harness 'preset'
$agent = Join-Path $preset 'agent.cordis.yml'
$meta = Join-Path $preset 'preset.yml'
$skills = Join-Path $preset 'skills'
$failed = $false

function Check($ok, $msg) {
    if ($ok) { Write-Output "[OK] $msg" }
    else { Write-Output "[FAIL] $msg"; $script:failed = $true }
}

Write-Output '==== GOAI agent preset verification ===='

Check (Test-Path $agent) 'template agent.cordis.yml exists'
Check (Test-Path $meta) 'template preset.yml exists'

if ((Test-Path $agent) -and (Test-Path $meta)) {
    $agentText = Get-Content $agent -Raw -Encoding UTF8
    $metaText = Get-Content $meta -Raw -Encoding UTF8

    Check ($metaText -match '(?s)name:\s*GOAI Options Terminal') 'preset.yml name = GOAI Options Terminal'
    Check ($metaText -match 'goai_state') 'preset description mentions goai_state'
    Check ($metaText -match 'python -m src') 'preset description documents CLI fallback'
    Check ($metaText -match 'tool-cordis 默认禁用') 'preset description states tool-cordis disabled'
    Check ($agentText -match "id:\s*tool-vision") 'tool-vision row present'
    Check ($agentText -match '数字铁律') 'GOAI persona iron rules present'
    Check ($agentText -match 'NO_TRADE') 'NO_TRADE-is-success discipline present'

    $cordisBlock = [regex]::Match($agentText, '(?s)- id: tool-cordis.*?name: ''@deepseek-ai/dsh-tool-cordis''(.*?)(?:\n- id: |\z)')
    Check ($cordisBlock.Success -and $cordisBlock.Groups[1].Value -match 'disabled:\s*true') 'tool-cordis disabled by default (registry collision fix)'

    $delegBlock = [regex]::Match($agentText, '(?s)- id: delegation.*?name: cordis:group(.*?)(?:config:|\n\s+isolate:)')
    Check ($delegBlock.Success -and $delegBlock.Groups[1].Value -match 'disabled:\s*true') 'delegation group disabled by default (lean product surface)'
}

# installed preset comparison
$dshHome = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $env:USERPROFILE '.dsh' }
$installed = Join-Path $dshHome '.agent-presets\goai-options'
Write-Output "[INFO] installed preset path: $installed"

if (-not (Test-Path $installed)) {
    Write-Output '[WARN] installed preset not found; run with -Sync to install'
    $failed = $true
} elseif ($Sync) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    foreach ($name in @('agent.cordis.yml', 'preset.yml')) {
        $dst = Join-Path $installed $name
        if (Test-Path $dst) {
            Copy-Item $dst (Join-Path $installed "$name.bak-$stamp") -Force
        }
        Copy-Item (Join-Path $preset $name) $dst -Force
    }
    $dstSkills = Join-Path $installed 'skills'
    if (Test-Path $dstSkills) { Remove-Item $dstSkills -Recurse -Force }
    Copy-Item $skills $dstSkills -Recurse -Force
    Write-Output '[OK] synced installed preset from template (backups written)'
}

if (Test-Path $installed) {
    $agentInst = Join-Path $installed 'agent.cordis.yml'
    $metaInst = Join-Path $installed 'preset.yml'
    $skillsInst = Join-Path $installed 'skills'
    Check (Test-Path $agentInst) 'installed agent.cordis.yml exists'
    Check (Test-Path $metaInst) 'installed preset.yml exists'
    Check (Test-Path $skillsInst) 'installed skills dir exists'

    if ((Test-Path $agentInst) -and (Test-Path $agent)) {
        $sameAgent = (Get-FileHash $agent -Algorithm SHA256).Hash -eq (Get-FileHash $agentInst -Algorithm SHA256).Hash
        Check $sameAgent 'installed agent.cordis.yml byte-identical to template'
    }
    if ((Test-Path $metaInst) -and (Test-Path $meta)) {
        $sameMeta = (Get-FileHash $meta -Algorithm SHA256).Hash -eq (Get-FileHash $metaInst -Algorithm SHA256).Hash
        Check $sameMeta 'installed preset.yml byte-identical to template'
    }
    if ((Test-Path $skillsInst) -and (Test-Path $skills)) {
        $templateFiles = Get-ChildItem $skills -Recurse -File | ForEach-Object { $_.FullName.Substring($skills.Length) }
        $installedFiles = Get-ChildItem $skillsInst -Recurse -File | ForEach-Object { $_.FullName.Substring($skillsInst.Length) }
        $sameList = (Compare-Object ($templateFiles | Sort-Object) ($installedFiles | Sort-Object)) -eq $null
        $sameBytes = $true
        if ($sameList) {
            foreach ($rel in $templateFiles) {
                $h1 = (Get-FileHash (Join-Path $skills $rel) -Algorithm SHA256).Hash
                $h2 = (Get-FileHash (Join-Path $skillsInst $rel) -Algorithm SHA256).Hash
                if ($h1 -ne $h2) { $sameBytes = $false; break }
            }
        }
        Check ($sameList -and $sameBytes) 'installed skills byte-identical to template'
    }
}

# environment check: experimental tool-search hides preset-scoped tools entirely.
# If the web profile has the plugin installed but our disabling patch is absent,
# warn loudly (do not fail the static preset check; fresh DSH installs may not
# have tool-search at all).
$webToolSearch = Join-Path $dshHome 'profiles\web\node_modules\@deepseek-ai\dsh-tool-search'
$webPatchFile = Join-Path $dshHome 'profiles\web\cordis.patch.yml'
$fixMarker = '# GOAI preset fix (added by harness\\fix_dsh_tool_visibility.ps1) - start'
if ((Test-Path $webToolSearch) -and (Test-Path $webPatchFile)) {
    $webPatchText = Get-Content $webPatchFile -Raw -Encoding UTF8
    if ($webPatchText -notmatch [regex]::Escape($fixMarker)) {
        Write-Output '[WARN] DSH web profile has tool-search installed but the GOAI visibility fix is not applied; preset tools (pwsh/read/write/view_image/...) will be invisible in new sessions. Run: powershell -ExecutionPolicy Bypass -File harness\\fix_dsh_tool_visibility.ps1'
    } else {
        Write-Output '[OK] DSH web tool-search fix present (new sessions get preset tools immediately)'
    }
} else {
    Write-Output '[INFO] tool-search not installed in DSH web profile; visibility fix not required'
}

if ($failed) {
    Write-Output '==== PRESET VERIFICATION FAILED (run with -Sync to repair drift) ===='
    exit 1
}
Write-Output '==== PRESET VERIFICATION PASSED ===='
