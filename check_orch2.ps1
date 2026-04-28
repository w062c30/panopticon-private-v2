$p = Get-Process -Id 71848 -ErrorAction SilentlyContinue
if ($p) {
    Write-Host "PID 71848 found: $($p.ProcessName) StartTime=$($p.StartTime)"
} else {
    Write-Host "PID 71848 NOT found"
}
Get-Process python | Select-Object Id, ProcessName, StartTime | Format-Table