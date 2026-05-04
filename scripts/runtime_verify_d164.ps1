# D164: Soak for z_ready > 0 in radar L1 logs (D75_ENTROPY_GATE is emitted by run_radar, not orchestrator).
# Run from repo root or any cwd — paths are relative to this script's parent (repo root).

$ErrorActionPreference = "Continue"
$repoRoot = Split-Path $PSScriptRoot -Parent
if (-not (Test-Path (Join-Path $repoRoot "panopticon_py"))) {
    Write-Error "Could not find panopticon_py under $repoRoot"
    exit 2
}
$radarLog = Join-Path $repoRoot "run\radar.err.log"
$orchLog = Join-Path $repoRoot "run\orchestrator.err.log"

Write-Host "[D164] repoRoot=$repoRoot"
Write-Host "[D164] Scanning radar.err.log (primary), then orchestrator.err.log — pattern z_ready:[1-9]"

$deadline = (Get-Date).AddMinutes(5)
$found = $false

while ((Get-Date) -lt $deadline) {
    foreach ($log in @($radarLog, $orchLog)) {
        if (-not (Test-Path $log)) { continue }
        $match = Select-String -Path $log -Pattern "z_ready:[1-9]\d*" | Select-Object -Last 3
        if ($match.Count -gt 0) {
            Write-Host "PASS: z_ready triggered in $(Split-Path $log -Leaf):"
            $match | ForEach-Object { Write-Host "  $_" }
            $found = $true
            break
        }
    }
    if ($found) { break }
    Start-Sleep -Seconds 15
}

if (-not $found) {
    Write-Host "FAIL: no z_ready:[1-9] within 5 minutes — tail D75_ENTROPY_GATE from radar log"
    if (Test-Path $radarLog) {
        Select-String -Path $radarLog -Pattern "D75_ENTROPY_GATE" | Select-Object -Last 8 | ForEach-Object { Write-Host $_ }
    } else {
        Write-Host "  (missing $radarLog — is radar running?)"
    }
    exit 1
}
exit 0
