$env:PANOPTICON_SHADOW = "1"
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logDir = "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = "$logDir/orchestrator_$timestamp.log"
Write-Host "Starting orchestrator with SHADOW=1, log: $logFile"
Start-Process -FilePath python -ArgumentList "run_hft_orchestrator.py" -WorkingDirectory "d:\Antigravity\Panopticon" -RedirectStandardOutput $logFile -RedirectStandardError $logFile -NoNewWindow -WindowStyle Hidden