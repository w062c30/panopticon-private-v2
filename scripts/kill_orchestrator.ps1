# kill_orchestrator.ps1
Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*run_hft*" -or $_.CommandLine -like "*orchestrat*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Write-Host "Orchestrator cleanup done"