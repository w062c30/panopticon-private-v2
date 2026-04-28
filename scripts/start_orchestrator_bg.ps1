$env:PANOPTICON_SHADOW = "1"
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logFile = "d:\Antigravity\Panopticon\logs\orchestrator_$timestamp.log"
$errFile = "d:\Antigravity\Panopticon\logs\orchestrator_$timestamp.err"
Write-Host "Log: $logFile"
$proc = Start-Process -FilePath python -ArgumentList "run_hft_orchestrator.py" -WorkingDirectory "d:\Antigravity\Panopticon" -PassThru -NoNewWindow
Write-Host "Started PID: $($proc.Id)"