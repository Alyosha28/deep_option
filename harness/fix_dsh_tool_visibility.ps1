# GOAI preset fix: disable experimental tool-search in the DSH web profile.
#
# Why: @deepseek-ai/dsh-tool-search only indexes GLOBAL tools. Agent-preset
# tools (pwsh/read/write/edit/glob/grep/skill/view_image/...) are registered in
# the preset scope layer, so with tool-search enabled they are never visible to
# the model and tool_search returns "No matching tools found". The goai-options
# preset therefore cannot run its documented CLI fallback or read engine files.
# Disabling the two tool-search rows restores the normal preset tool catalog
# (verified on a second DSH instance: 97 tools visible incl. pwsh/read/write).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File harness\\fix_dsh_tool_visibility.ps1        # apply
#   powershell -ExecutionPolicy Bypass -File harness\\fix_dsh_tool_visibility.ps1 -Undo  # remove
# New sessions pick up the fix immediately (verified on the running main
# instance 2026-08-15: fresh goai-options session got all 97 preset tools).
# Running sessions keep their old restriction; restarting DSH remains the
# deterministic fallback.
#   node harness\\smoke_preset.mjs
# ASCII-only on purpose (project pitfall #2).

param([switch]$Undo)

$ErrorActionPreference = 'Stop'
$dshHome = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $env:USERPROFILE '.dsh' }
$profilePatch = Join-Path $dshHome 'profiles\\web\\cordis.patch.yml'

if (-not (Test-Path $profilePatch)) {
    Write-Output "[FAIL] web profile patch not found: $profilePatch"
    exit 1
}

$start = '# GOAI preset fix (added by harness\\fix_dsh_tool_visibility.ps1) - start'
$end = '# GOAI preset fix - end'
$block = @"

$start
- id: tool-search
  disabled: true

- id: tool-search-invariant
  disabled: true
$end
"@

$text = Get-Content $profilePatch -Raw -Encoding UTF8
$hasBlock = $text.Contains($start)

if ($Undo) {
    if (-not $hasBlock) {
        Write-Output '[INFO] GOAI tool-search fix not present; nothing to undo'
        exit 0
    }
    $startIdx = $text.IndexOf($start)
    $endIdx = $text.IndexOf($end) + $end.Length
    $newText = $text.Remove($startIdx, $endIdx - $startIdx).TrimEnd()
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    Copy-Item $profilePatch ($profilePatch + '.bak-undo-' + $stamp) -Force
    [System.IO.File]::WriteAllText($profilePatch, $newText + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($true)))
    Write-Output '[OK] removed GOAI tool-search fix (backup written)'
    Write-Output '[INFO] new sessions pick up the change immediately; running sessions keep their old toolset'
    exit 0
}

if ($hasBlock) {
    Write-Output '[OK] GOAI tool-search fix already present'
} else {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    Copy-Item $profilePatch ($profilePatch + '.bak-goai-' + $stamp) -Force
    $newText = $text.TrimEnd() + [Environment]::NewLine + [Environment]::NewLine + $block.TrimEnd() + [Environment]::NewLine
    [System.IO.File]::WriteAllText($profilePatch, $newText, (New-Object System.Text.UTF8Encoding($true)))
    Write-Output '[OK] appended GOAI tool-search fix (backup written)'
}

Write-Output '[INFO] new sessions pick up the fix immediately (verified 2026-08-15); run: node harness\\smoke_preset.mjs'
Write-Output '[INFO] to roll back later: powershell -ExecutionPolicy Bypass -File harness\\fix_dsh_tool_visibility.ps1 -Undo'
