# === Panopticon Health Check ===
# Usage: powershell -ExecutionPolicy Bypass -File scripts/health_check.ps1

$ErrorActionPreference = "Continue"
Write-Host "=== Panopticon Health Check ===" -ForegroundColor Cyan

# 1. Version
Write-Host "`n[1] Versions:" -ForegroundColor Yellow
$versions = Get-Content "run\versions_ref.json" | ConvertFrom-Json
Write-Host "  radar: $($versions.radar)"
Write-Host "  orchestrator: $($versions.orchestrator)"
Write-Host "  updated_sprint: $($versions.updated_by_sprint)"

# 2. DB data accumulation
Write-Host "`n[2] DB Data Accumulation:" -ForegroundColor Yellow
python scripts/db_health.py

# 3. Error check
Write-Host "`n[3] Recent Errors:" -ForegroundColor Yellow
if (Test-Path "run\orchestrator.err.log") {
    $errTail = Get-Content "run\orchestrator.err.log" | Select-Object -Last 10
    if ($errTail) {
        $errTail | ForEach-Object { Write-Host $_ }
    } else {
        Write-Host "  (empty)" -ForegroundColor Green
    }
} else {
    Write-Host "  no orchestrator.err.log" -ForegroundColor Green
}

# 4. WS activity
Write-Host "`n[4] WS Activity:" -ForegroundColor Yellow
if (Test-Path "run\orchestrator.log") {
    $logTail = Get-Content "run\orchestrator.log" | Select-Object -Last 5
    $logTail | ForEach-Object { Write-Host $_ }
} else {
    Write-Host "  no orchestrator.log"
}

Write-Host "`n=== Health Check Complete ===" -ForegroundColor Cyan