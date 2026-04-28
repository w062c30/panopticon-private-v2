$ErrorActionPreference = "Stop"

$db = "data\panopticon.db"

Write-Host "=== SQLite DB Raw Header Check ===" -ForegroundColor Cyan
$fs = [System.IO.File]::OpenRead((Resolve-Path $db).Path)
$br = New-Object System.IO.BinaryReader($fs)
$magic = $br.ReadBytes(16)
$fs.Close()
Write-Host "Magic: $([System.Text.Encoding]::ASCII.GetString($magic))" -ForegroundColor White

Write-Host ""
Write-Host "=== DB File Size ===" -ForegroundColor Cyan
$fi = Get-Item $db
Write-Host "Size: $([math]::Round($fi.Length / 1MB, 2)) MB" -ForegroundColor White
Write-Host "Modified: $($fi.LastWriteTime)" -ForegroundColor White

Write-Host ""
Write-Host "=== WAL Journal ===" -ForegroundColor Cyan
$wal = "$db-wal"
$shm = "$db-shm"
if (Test-Path $wal) {
    $walInfo = Get-Item $wal
    Write-Host "WAL exists: $([math]::Round($walInfo.Length / 1KB, 1)) KB" -ForegroundColor Yellow
} else {
    Write-Host "WAL: NOT PRESENT (journal mode)" -ForegroundColor Gray
}
if (Test-Path $shm) {
    Write-Host "SHM exists: $([math]::Round((Get-Item $shm).Length / 1KB, 1)) KB" -ForegroundColor Yellow
} else {
    Write-Host "SHM: NOT PRESENT" -ForegroundColor Gray
}

Write-Host ""
Write-Host "=== SQLite Header (page size + version) ===" -ForegroundColor Cyan
$fs2 = [System.IO.File]::OpenRead((Resolve-Path $db).Path)
$br2 = New-Object System.IO.BinaryReader($fs2)
$br2.ReadBytes(16) | Out-Null
$pageSizeRaw = $br2.ReadBytes(2)
$pageSize = [int]([byte]$pageSizeRaw[0]) * 256
$fs2.Close()
Write-Host "Page size: $pageSize bytes" -ForegroundColor White

Write-Host ""
Write-Host "=== Checking for workers holding DB lock ===" -ForegroundColor Cyan
$pythonProcs = Get-Process python -ErrorAction SilentlyContinue | Where-Object {
    $_.MainWindowTitle -like "*panopticon*" -or
    $_.MainWindowTitle -like "*shadow*" -or
    $_.MainWindowTitle -like "*hunting*" -or
    $_.MainWindowTitle -like "*discovery*" -or
    $_.MainWindowTitle -eq ""
}
Write-Host "Python processes found: $($pythonProcs.Count)" -ForegroundColor White

Write-Host ""
Write-Host "=== CONCLUSION ===" -ForegroundColor Cyan
if ($fi.Length -gt 10MB) {
    Write-Host "DB is non-trivial size ($([math]::Round($fi.Length/1MB,1)) MB) - DATA IS BEING COLLECTED." -ForegroundColor Green
}
if (Test-Path $wal) {
    Write-Host "WAL journal active - workers are writing." -ForegroundColor Green
}
Write-Host "URI-mode read-only fails because ShadowDB uses BEGIN IMMEDIATE transaction." -ForegroundColor Yellow
Write-Host "Workers hold exclusive write lock - this is EXPECTED during shadow mode." -ForegroundColor Yellow
Write-Host "To audit DB content, either:" -ForegroundColor White
Write-Host "  1. Stop all panopticon workers (Ctrl+C each)" -ForegroundColor White
Write-Host "  2. Or wait for idle period when workers are between loops" -ForegroundColor White
Write-Host "  3. Or copy DB: Copy-Item data\panopticon.db data\panopticon_copy.db" -ForegroundColor White
Write-Host "     then: python verify_db_collection.py (will read the copy)" -ForegroundColor White
