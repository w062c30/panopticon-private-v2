Write-Host "Current PID (uppercase, readonly):" $PID
$myPid = 12345
Write-Host "myPid (lowercase) before:" $myPid
$myPid = 55555
Write-Host "myPid (lowercase) after assignment:" $myPid
Write-Host "PID (uppercase) unchanged:" $PID
Write-Host "SUCCESS: lowercase pid is writable, uppercase PID is readonly"
