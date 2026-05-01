Get-ChildItem | Where-Object {
    $name = $_.Name
    $match = $name -match '^(COMMIT_MSG|_|check_|diag_|test_marker|full_signal)'
    if ($match) { Write-Host "Removing: $name" }
    $match
} | Remove-Item -Force -ErrorAction SilentlyContinue

Get-ChildItem -Filter "*.py" | Where-Object {
    $_.Name -match '^(check_|diag_|test_|full_signal)'
} | ForEach-Object {
    Write-Host "Removing: $($_.Name)"
    Remove-Item $_.FullName -Force
}

Write-Host "`nRemaining .py files in root:"
Get-ChildItem -Filter "*.py" | Select-Object Name | Format-Table -AutoSize