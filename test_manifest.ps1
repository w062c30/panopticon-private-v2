$m = Get-Content 'd:\Antigravity\Panopticon\run\process_manifest.json' | ConvertFrom-Json
foreach ($svc in @('backend', 'radar', 'orchestrator')) {
    $e = $m.PSObject.Properties[$svc].Value
    if ($null -ne $e) {
        Write-Host "$svc : PID=$($e.pid) version=$($e.version)"
    } else {
        Write-Host "$svc : not found"
    }
}
