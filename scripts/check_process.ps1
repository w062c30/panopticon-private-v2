# check_orchestrator.ps1
$procs = Get-Process python -ErrorAction SilentlyContinue
foreach ($p in $procs) {
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)" -ErrorAction SilentlyContinue).CommandLine
        if ($cmd -like "*run_hft*" -or $cmd -like "*orchestrat*") {
            Write-Host "Found orchestrator: PID=$($p.Id) Cmd=$cmd"
        }
    } catch {}
}
Write-Host "Done checking processes"