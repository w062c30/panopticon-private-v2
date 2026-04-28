# Schedule Panopticon insight report every 2 hours
# Run as: powershell -File scripts/schedule_insight_report.ps1
$action = New-ScheduledTaskAction -Execute "python" -Argument "scripts\report_insights.py -o data\insight_reports\report_$(Get-Date -Format 'yyyyMMdd_HHmmss').json"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 2) -RepetitionDuration ([TimeSpan]::MaxValue)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName "PanopticonInsightReport" -Action $action -Trigger $trigger -Settings $settings -Description "Panopticon shadow mode insight report every 2 hours" -Force
Write-Host "Scheduled task 'PanopticonInsightReport' registered. Runs every 2 hours."
Write-Host "To view scheduled tasks: Get-ScheduledTask | Where-Object {$_.TaskName -like '*Panopticon*'}"
Write-Host "To unregister: Unregister-ScheduledTask -TaskName 'PanopticonInsightReport' -Confirm:`$false"
