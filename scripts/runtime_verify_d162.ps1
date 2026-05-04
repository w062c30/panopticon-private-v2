# D162: Runtime acceptance — PRAGMA retry, analysis_worker, Kyle GUARD_* diagnostics
# Run after: restart_all.ps1 + soak (set LOG_LEVEL=DEBUG on orchestrator to see GUARD_*)
# Usage: powershell -File scripts\runtime_verify_d162.ps1
# Note: named runtime_verify_* because check_*.ps1 is gitignored.

$ErrorActionPreference = "Continue"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RunDir = Join-Path $RepoRoot "run"
$ManifestPath = Join-Path $RunDir "process_manifest.json"
$AwErr = Join-Path $RunDir "analysis_worker.err.log"
$OrchLog = Join-Path $RunDir "orchestrator.log"

Write-Host "=== D162 Runtime Check ===" -ForegroundColor Cyan
Write-Host "Repo: $RepoRoot" -ForegroundColor DarkGray
Write-Host ""

Write-Host "=== 1. analysis_worker (process_manifest.json) ===" -ForegroundColor Yellow
if (Test-Path $ManifestPath) {
    $manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
    $aw = $manifest.analysis_worker
    if ($null -eq $aw) {
        Write-Warning "FAIL: analysis_worker missing from manifest"
    } else {
        $st = $aw.status
        Write-Host "  status: $st"
        if ($st -ne "running") { Write-Warning "FAIL: analysis_worker not running" }
    }
} else {
    Write-Warning "manifest not found: $ManifestPath"
}

Write-Host "`n=== 2. analysis_worker.err.log (database is locked) ===" -ForegroundColor Yellow
if (Test-Path $AwErr) {
    $locked = Select-String -Path $AwErr -Pattern "database is locked"
    Write-Host "  Count: $($locked.Count)"
    if ($locked.Count -gt 0) {
        Write-Warning "FAIL: DB locked errors in analysis_worker log"
        $locked | Select-Object -Last 3 | ForEach-Object { Write-Host "  $($_.Line)" }
    }
} else {
    Write-Host "  (no analysis_worker.err.log yet)" -ForegroundColor DarkGray
}

Write-Host "`n=== 3. Kyle GUARD_A / GUARD_B (orchestrator.log, last 5) ===" -ForegroundColor Yellow
if (Test-Path $OrchLog) {
    $guard = Select-String -Path $OrchLog -Pattern "\[KYLE\]\[GUARD_" | Select-Object -Last 5
    if ($guard) {
        $guard | ForEach-Object { Write-Host "  $($_.Line)" }
    } else {
        Write-Host "  (none — set LOG_LEVEL=DEBUG on orchestrator to emit GUARD logs)" -ForegroundColor DarkGray
    }
} else {
    Write-Host "  orchestrator.log not found" -ForegroundColor DarkGray
}

Write-Host "`n=== 4. SQLite (execution_records + insider_score_snapshots) ===" -ForegroundColor Yellow
Push-Location $RepoRoot
try {
    python -c @"
import os, sqlite3
from datetime import datetime, timezone, timedelta
db_path = os.getenv('PANOPTICON_DB_PATH', 'data/panopticon.db')
conn = sqlite3.connect(db_path)
total = conn.execute('SELECT COUNT(*) FROM execution_records').fetchone()[0]
by_gate = conn.execute(
    'SELECT gate_reason, COUNT(*) AS c FROM execution_records GROUP BY gate_reason ORDER BY c DESC'
).fetchall()
cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace('+00:00', 'Z')
aw_snaps = conn.execute(
    'SELECT COUNT(*) FROM insider_score_snapshots WHERE ingest_ts_utc >= ?',
    (cutoff,),
).fetchone()[0]
print(f'  execution_records total: {total}')
for row in by_gate:
    print(f'    gate={row[0]}: {row[1]}')
print(f'  insider_score_snapshots (ingest_ts_utc >= 1h ago): {aw_snaps}')
conn.close()
"@
} finally {
    Pop-Location
}

Write-Host "`n=== D162 Pass criteria (manual) ===" -ForegroundColor Cyan
Write-Host "  1. analysis_worker status = running"
Write-Host "  2. No new database is locked in analysis_worker.err.log after D162 deploy"
Write-Host "  3. insider_score_snapshots (last 1h) grows when worker is healthy"
Write-Host "  4. GUARD_* lines appear when LOG_LEVEL=DEBUG"
