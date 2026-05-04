# D164: Soak for z_ready > 0 in radar L1 logs (D75_ENTROPY_GATE is emitted by run_radar, not orchestrator).
# Run from repo root or any cwd — paths are relative to this script's parent (repo root).

$ErrorActionPreference = "Continue"
$repoRoot = Split-Path $PSScriptRoot -Parent
if (-not (Test-Path (Join-Path $repoRoot "panopticon_py"))) {
    Write-Error "Could not find panopticon_py under $repoRoot"
    exit 2
}
# D79: L1 radar runs inside orchestrator — D75_ENTROPY_GATE is on orchestrator stderr first.
$radarLog = Join-Path $repoRoot "run\radar.err.log"
$orchLog = Join-Path $repoRoot "run\orchestrator.err.log"

Write-Host "[D164] repoRoot=$repoRoot"
Write-Host "[D164] Pass if (A) D75 gate line has z_ready count >0 in a minute, OR (B) data/entropy_status.json z_ready_count >= 1"
Write-Host "[D164] Note: D75 z_ready is 60s eval counter; JSON z_ready_count is per-token aggregate — they can diverge."

$snapPath = Join-Path $repoRoot "data\entropy_status.json"

function Test-EntropySnapshotZReady {
    if (-not (Test-Path $snapPath)) { return $false }
    try {
        $j = Get-Content -Raw -Path $snapPath | ConvertFrom-Json
        $n = [int]($j.z_ready_count)
        return ($n -ge 1)
    } catch {
        return $false
    }
}

$deadline = (Get-Date).AddMinutes(5)
$found = $false

while ((Get-Date) -lt $deadline) {
    if (Test-EntropySnapshotZReady) {
        Write-Host "PASS: data/entropy_status.json z_ready_count >= 1"
        try {
            $j = Get-Content -Raw -Path $snapPath | ConvertFrom-Json
            Write-Host "  z_ready_count=$($j.z_ready_count) total=$($j.total) updated=$($j.updated_ts)"
        } catch {}
        $found = $true
        break
    }
    foreach ($log in @($orchLog, $radarLog)) {
        if (-not (Test-Path $log)) { continue }
        # Minute gate: gate_60s={... z_ready:N ...} — N>0 means at least one z computed in that window
        $match = Select-String -Path $log -Pattern "gate_60s=\{[^}]*z_ready:[1-9]\d*" | Select-Object -Last 3
        if ($match.Count -gt 0) {
            Write-Host "PASS: D75 z_ready minute counter >0 in $(Split-Path $log -Leaf):"
            $match | ForEach-Object { Write-Host "  $_" }
            $found = $true
            break
        }
    }
    if ($found) { break }
    Start-Sleep -Seconds 15
}

if (-not $found) {
    Write-Host "FAIL: no z_ready:[1-9] within 5 minutes — tail D75_ENTROPY_GATE (orchestrator first)"
    foreach ($log in @($orchLog, $radarLog)) {
        if (-not (Test-Path $log)) { continue }
        Write-Host "--- $(Split-Path $log -Leaf) ---"
        Select-String -Path $log -Pattern "D75_ENTROPY_GATE" | Select-Object -Last 8 | ForEach-Object { Write-Host $_ }
    }
    exit 1
}
exit 0
