# D163: Monitor analysis_worker.err.log for LIFO / commit regressions (5-minute window)
# Usage: powershell -File scripts\runtime_verify_d163.ps1

$ErrorActionPreference = "Continue"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$AwErr = Join-Path $RepoRoot "run\analysis_worker.err.log"

Write-Host "=== D163: 5-minute error watch (analysis_worker.err.log) ===" -ForegroundColor Cyan
Write-Host "Log: $AwErr" -ForegroundColor DarkGray

if (-not (Test-Path $AwErr)) {
    Write-Warning "analysis_worker.err.log not found — start analysis_worker first"
    exit 1
}

$patterns = @(
    "cannot commit",
    "error return without exception",
    "\[ANALYSIS_WORKER\] tick failed"
)
$combined = ($patterns | ForEach-Object { "($_)" }) -join "|"

$start = Get-Date
$deadline = $start.AddMinutes(5)
$iteration = 0
while ((Get-Date) -lt $deadline) {
    $iteration++
    $matches = Select-String -Path $AwErr -Pattern $combined -ErrorAction SilentlyContinue
    $recent = $matches | Where-Object { $_.Line -match "." } | Select-Object -Last 5
    Write-Host ("[{0:HH:mm:ss}] window scan: total pattern matches in file={1}" -f (Get-Date), $matches.Count)
    if ($recent) {
        Write-Warning "Recent hits (review):"
        $recent | ForEach-Object { Write-Host "  $($_.Line)" }
    } else {
        Write-Host "  (no matching lines in full file this pass)" -ForegroundColor DarkGreen
    }
    Start-Sleep -Seconds 30
}

Write-Host "`nDone. Pass criterion: no NEW tick failures during soak — compare log tail manually." -ForegroundColor Cyan
