# D164 / D165: Soak for z_eval_ok > 0 in D75 ENTROPY_GATE (run inside orchestrator).
# D165 naming: z_eval_ok = 60s counter of tokens whose zscore_of_latest_delta() returned finite.
# Backward compat: also accepts old "z_ready" name for pre-D165 logs.
# Run from repo root or any cwd — paths are relative to this script's parent (repo root).

$ErrorActionPreference = "Continue"
$repoRoot = Split-Path $PSScriptRoot -Parent
if (-not (Test-Path (Join-Path $repoRoot "panopticon_py"))) {
    Write-Error "Could not find panopticon_py under $repoRoot"
    exit 2
}
$radarLog = Join-Path $repoRoot "run\radar.err.log"
$orchLog = Join-Path $repoRoot "run\orchestrator.err.log"

Write-Host "[D165] repoRoot=$repoRoot"
Write-Host "[D165] Pass if (A) D75 gate line has z_eval_ok >= 1, OR (B) entropy_status.json has unlocked+ready token"
Write-Host "[D165] Note: D75 z_eval_ok = 60s eval counter; JSON z_ready = per-token flag — they can diverge."

$snapPath = Join-Path $repoRoot "data\entropy_status.json"

function Test-EntropySnapshotZReady {
    if (-not (Test-Path $snapPath)) { return $false }
    try {
        $j = Get-Content -Raw -Path $snapPath | ConvertFrom-Json
        # PASS-B: at least one token that is both z_ready=true AND trigger_locked=false
        $ok = ($j.tokens | Where-Object { $_.z_ready -eq $true -and $_.trigger_locked -eq $false }).Count -ge 1
        return $ok
    } catch {
        return $false
    }
}

$deadline = (Get-Date).AddMinutes(5)
$found = $false

while ((Get-Date) -lt $deadline) {
    if (Test-EntropySnapshotZReady) {
        Write-Host "PASS-B: entropy_status.json has unlocked+ready token"
        try {
            $j = Get-Content -Raw -Path $snapPath | ConvertFrom-Json
            $unlocked = ($j.tokens | Where-Object { $_.z_ready -eq $true -and $_.trigger_locked -eq $false }).Count
            Write-Host "  unlocked_ready=$unlocked / total=$($j.total) updated=$($j.updated_ts)"
        } catch {}
        $found = $true
        break
    }
    foreach ($log in @($orchLog, $radarLog)) {
        if (-not (Test-Path $log)) { continue }
        # D165: z_eval_ok (new) takes priority; z_ready (old, pre-D165) as backward compat
        $match = Select-String -Path $log -Pattern "gate_60s=\{[^}]*z_eval_ok:[1-9]\d*" | Select-Object -Last 3
        if ($match.Count -eq 0) {
            $match = Select-String -Path $log -Pattern "gate_60s=\{[^}]*z_ready:[1-9]\d*" | Select-Object -Last 3
        }
        if ($match.Count -gt 0) {
            Write-Host "PASS-A: D75 z_eval_ok triggered in $(Split-Path $log -Leaf):"
            $match | ForEach-Object { Write-Host "  $_" }
            $found = $true
            break
        }
    }
    if ($found) { break }
    Start-Sleep -Seconds 15
}

if (-not $found) {
    Write-Host "FAIL: no z_eval_ok >= 1 AND no unlocked+ready token after 5 minutes"
    foreach ($log in @($orchLog, $radarLog)) {
        if (-not (Test-Path $log)) { continue }
        Write-Host "--- $(Split-Path $log -Leaf) D75 tail ---"
        Select-String -Path $log -Pattern "D75_ENTROPY_GATE" | Select-Object -Last 6 | ForEach-Object { Write-Host $_ }
    }
    Write-Host "--- token summary (entropy_status.json) ---"
    if (Test-Path $snapPath) {
        try {
            $j = Get-Content -Raw -Path $snapPath | ConvertFrom-Json
            $j.tokens | Select-Object @{N="locked";E={$_.trigger_locked}},
                @{N="z_ready";E={$_.z_ready}},
                h_hist, events, healthy_span |
                Sort-Object locked, z_ready | Select-Object -First 20 | Format-Table -AutoSize
        } catch {}
    }
    exit 1
}
exit 0
