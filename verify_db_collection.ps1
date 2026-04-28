param(
    [string]$DBPath = "data\panopticon.db",
    [int]$TimeoutSec = 5
)

$ErrorActionPreference = "Stop"
[void][System.Reflection.Assembly]::LoadFrom((Resolve-Path ".\packages\System.Data.SQLite.dll" -ErrorAction SilentlyContinue).Path)
if (-not ([System.Management.Automation.PSTypeName]'System.Data.SQLite.SQLiteConnection').Type) {
    try {
        Add-Type -Path "$env:USERPROFILE\.nuget\packages\system.data.sqlite.core\*\lib\netstandard2.1\System.Data.SQLite.dll" -ErrorAction SilentlyContinue
    } catch {}
}

$script:conn = $null

try {
    $conn = New-Object System.Data.SQLite.SQLiteConnection
    $conn.ConnectionString = "Data Source=$DBPath;Mode=ReadOnly;BusyTimeout=3000"
    $conn.Open()

    Write-Host "=== DB Collection Audit: $DBPath ===" -ForegroundColor Cyan
    Write-Host ""

    $tables = @(
        "raw_events",
        "strategy_decisions",
        "execution_records",
        "positions",
        "collateral_reservations",
        "correlation_edges",
        "watched_wallets",
        "wallet_observations",
        "insider_score_snapshots",
        "hunting_shadow_hits",
        "paper_trades",
        "realized_pnl_settlement",
        "virtual_entity_events",
        "discovered_entities",
        "tracked_wallets",
        "audit_log",
        "polymarket_link_map",
        "polymarket_link_unresolved"
    )

    $allOk = $true
    foreach ($tbl in $tables) {
        try {
            $cmd = $conn.CreateCommand()
            $cmd.CommandText = "SELECT COUNT(*) FROM $tbl"
            $count = [int]$cmd.ExecuteScalar()
            if ($count -eq 0) {
                Write-Host "[EMPTY]  $tbl ($count rows)" -ForegroundColor Yellow
            } else {
                Write-Host "[OK]     $tbl ($count rows)" -ForegroundColor Green
            }
        } catch {
            Write-Host "[ISSUE]  $tbl - $_" -ForegroundColor Red
            $allOk = $false
        }
    }

    Write-Host ""
    Write-Host "=== Key Statistics ===" -ForegroundColor Cyan

    $queries = @{
        "raw_events_by_layer"      = "SELECT layer, event_type, COUNT(*) as cnt FROM raw_events GROUP BY layer, event_type ORDER BY cnt DESC LIMIT 10"
        "wallet_obs_by_type"       = "SELECT obs_type, COUNT(*) as cnt FROM wallet_observations GROUP BY obs_type ORDER BY cnt DESC LIMIT 10"
        "hunting_hits_outcome"    = "SELECT outcome, COUNT(*) as cnt FROM hunting_shadow_hits GROUP BY outcome ORDER BY cnt DESC LIMIT 10"
        "exec_accepted_rejected"  = "SELECT accepted, COUNT(*) as cnt FROM execution_records GROUP BY accepted"
        "entities_by_tag"         = "SELECT primary_tag, COUNT(*) as cnt FROM discovered_entities GROUP BY primary_tag ORDER BY cnt DESC"
        "shadow_hit_rate"         = "SELECT COUNT(*) as total, SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins FROM hunting_shadow_hits WHERE outcome IN ('win','loss')"
        "recent_decisions"       = "SELECT action, COUNT(*) as cnt FROM strategy_decisions GROUP BY action ORDER BY cnt DESC LIMIT 5"
    }

    foreach ($key in $queries.Keys) {
        try {
            $cmd = $conn.CreateCommand()
            $cmd.CommandText = $queries[$key]
            $reader = $cmd.ExecuteReader()
            Write-Host ""
            Write-Host "  $key :" -ForegroundColor Magenta
            while ($reader.Read()) {
                $vals = @()
                for ($i = 0; $i -lt $reader.FieldCount; $i++) {
                    $v = $reader[$i]
                    if ($v -is [System.DBNull]) { $v = "NULL" }
                    $vals += "$v"
                }
                Write-Host "    $($vals -join ' | ')" -ForegroundColor White
            }
            $reader.Close()
        } catch {
            Write-Host "  [ERR] $key : $_" -ForegroundColor Red
        }
    }

    $conn.Close()

    Write-Host ""
    if ($allOk) {
        Write-Host "=== ALL TABLES OK ===" -ForegroundColor Green
    } else {
        Write-Host "=== SOME TABLES: ISSUES FOUND ===" -ForegroundColor Red
        exit 1
    }

} catch {
    $errMsg = $_.Exception.Message
    if ($errMsg -like "*locked*") {
        Write-Host "" -ForegroundColor Yellow
        Write-Host "[INFO] Database is LOCKED - ShadowDB has exclusive write access." -ForegroundColor Yellow
        Write-Host "This is EXPECTED if Shadow Mode workers are running." -ForegroundColor Yellow
        Write-Host "Set PANOPTICON_SHADOW_MODE=0 to pause workers, then re-run." -ForegroundColor Yellow
        Write-Host "Raw error: $errMsg" -ForegroundColor Gray
        exit 0
    }
    Write-Host "FATAL: $errMsg" -ForegroundColor Red
    if ($conn -and $conn.State -eq "Open") { $conn.Close() }
    exit 1
}
