Write-Host "Current PID:" $PID
$pid = 99999
Write-Host "After assigning 99999 to $pid, PID value is:" $PID
Write-Host "Conclusion: $pid and $PID point to the SAME variable (case-insensitive)"
if ($PID -eq 99999) {
    Write-Host "CONFIRMED: $pid IS $PID - the warning is legitimate"
}
