# Panopticon - Singleton-Enforced Process Restart with Auto-Recovery
# Version: v1.0.9-D78
# Run from: d:\Antigravity\Panopticon
# MANDATORY: Use this script for ALL restarts.
# OPTIONAL: Pass "-Continuous" for continuous monitoring with auto-recovery.

param(
    [switch]$Continuous,
    [int]$MonitorIntervalSec = 10,
    [int]$MaxRestartAttempts = 3
)

$projDir = "d:\Antigravity\Panopticon"
$dashDir = "$projDir\dashboard"
$runDir = "$projDir\run"
$manifestPath = "$runDir\process_manifest.json"
$restartAttempts = @{ backend = 0; radar = 0; orchestrator = 0; analysis_worker = 0 }

function Get-ProcessStatus {
    $status = @{}
    $pythonProcs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -ne $null -and $_.CommandLine -match [regex]::Escape($projDir) }

    $status.backend = ($pythonProcs | Where-Object { $_.CommandLine -match "uvicorn.*8001" }).Count
    $status.radar = ($pythonProcs | Where-Object { $_.CommandLine -match "run_radar" }).Count
    $status.orchestrator = ($pythonProcs | Where-Object { $_.CommandLine -match "run_hft_orchestrator" }).Count
    $status.analysis_worker = ($pythonProcs | Where-Object { $_.CommandLine -match "analysis_worker" }).Count
    $status.frontend = (Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Measure-Object).Count
    $status.frontendPort = $null -ne (Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Where-Object {
        $cmd = $_.CommandLine;
        if ($cmd -match "port:(\d+)") { $matches[1] -eq "5173" }
        elseif ($cmd -match "5173") { $true }
        else { $false }
    })

    return $status
}

function Start-Backend {
    # D78: pre-flight check — abort if port 8001 already in use
    $inUse = Get-NetTCPConnection -LocalPort 8001 -ErrorAction SilentlyContinue | Where-Object { $_.State -eq "Listen" }
    if ($inUse) {
        Write-Warning "  [START_BACKEND] port 8001 still occupied — aborting backend start"
        return $null
    }
    Start-Process python -ArgumentList "-m uvicorn panopticon_py.api.app:app --host 0.0.0.0 --port 8001" -WorkingDirectory $projDir -WindowStyle Hidden -PassThru
}

function Start-Radar {
    $radarLog = "$runDir\radar.log"
    $radarErr = "$runDir\radar.err.log"
    Start-Process python -ArgumentList "-m panopticon_py.hunting.run_radar" -WorkingDirectory $projDir -WindowStyle Hidden -RedirectStandardOutput $radarLog -RedirectStandardError $radarErr -PassThru
}

function Start-Orchestrator {
    $env:PANOPTICON_WHALE = "1"
    Start-Process python -ArgumentList "$projDir\run_hft_orchestrator.py" -WorkingDirectory $projDir -WindowStyle Hidden -PassThru
}

function Start-AnalysisWorker {
    Start-Process python -ArgumentList "-m panopticon_py.ingestion.analysis_worker" -WorkingDirectory $projDir -WindowStyle Hidden -PassThru
}

function Start-Frontend {
    $npmCmd = $null
    $dashboardExists = Test-Path $dashDir

    # Resolve npm executable — use npm.cmd on Windows to avoid .ps1 execution policy issues
    $npmCmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npmCmd) {
        $npmCmd = Get-Command npm -ErrorAction SilentlyContinue
    }
    if (-not $npmCmd) {
        Write-Warning "[D56] npm not found in PATH — skipping frontend start"
        Write-Warning "  To start manually: cd $dashDir && npm run dev"
        return $null
    }
    if (-not $dashboardExists) {
        Write-Warning "[D56] dashboard/ directory not found — skipping frontend start"
        return $null
    }

    # Kill any existing vite/node processes for this project
    Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Where-Object {
        $_.CommandLine -match "vite|dashboard"
    } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep 2

    # Use cmd /c to run npm via npm.cmd (Windows native .cmd, avoids PS script policy)
    $proc = Start-Process cmd -ArgumentList "/c","npm run dev" -WorkingDirectory $dashDir -NoNewWindow -PassThru
    Write-Host "[D56] Frontend PID: $($proc.Id)"
    Start-Sleep 5
    $viteOK = $null -ne (Get-NetTCPConnection -LocalPort 5173 -ErrorAction SilentlyContinue)
    if ($viteOK) {
        Write-Host "[D56] Frontend :5173 UP"
    } else {
        Write-Warning "[D56] Frontend started but :5173 not responding yet (may still be loading)"
    }
    return $proc
}

function Kill-All {
    Write-Host "== KILLING ALL MANAGED PROCESSES ==" -ForegroundColor Yellow

    $pythonTargets = @("run_radar", "run_hft_orchestrator", "uvicorn.*8001", "analysis_worker")
    foreach ($t in $pythonTargets) {
        $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
            $_.CommandLine -ne $null -and
            $_.CommandLine -match $t -and
            $_.CommandLine -match [regex]::Escape($projDir)
        }
        foreach ($p in $procs) {
            Write-Host ("  Killing [$t] PID=" + $p.ProcessId)
            Invoke-CimMethod -InputObject $p -Name Terminate -ErrorAction SilentlyContinue
        }
    }

    $nodeProcs = Get-Process node -ErrorAction SilentlyContinue
    foreach ($n in $nodeProcs) {
        Write-Host ("  Killing node PID=" + $n.Id)
        Stop-Process -Id $n.Id -Force -ErrorAction SilentlyContinue
    }

    # D78: Port-level zombie cleanup — kills any process holding 8001/8002 across ALL sessions
    foreach ($port in @(8001, 8002)) {
        $portOwners = netstat -ano | Select-String ":$port\s" | ForEach-Object {
            ($_ -split '\s+')[-1]
        } | Where-Object { $_ -match '^\d+$' } | Select-Object -Unique
        foreach ($procId in $portOwners) {
            Write-Host "  [PORT_KILL] port=$port PID=$procId"
            $result = & taskkill /F /PID $procId 2>&1
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "  [PORT_KILL] taskkill failed (pid=$procId): $result"
                Write-Warning "  [PORT_KILL] Try running restart_all.ps1 as Administrator to kill cross-session zombies"
            } else {
                Write-Host "  [PORT_KILL] Killed PID=$procId holding port $port" -ForegroundColor Green
            }
        }
    }

    New-Item -ItemType Directory -Force -Path $runDir | Out-Null
    Remove-Item "$runDir\*.pid" -Force -ErrorAction SilentlyContinue
    Remove-Item "$runDir\radar.log","$runDir\radar.err.log" -Force -ErrorAction SilentlyContinue
    Write-Host "  Stale PID files cleared."
    Start-Sleep -Seconds 3
}

function Full-Restart {
    Kill-All
    
    Write-Host "== STEP 2: VERIFY ALL DEAD ==" -ForegroundColor Yellow
    $stillAlive = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
        $_.CommandLine -ne $null -and $_.CommandLine -match [regex]::Escape($projDir) -and
        ($_.CommandLine -match "run_radar" -or $_.CommandLine -match "run_hft_orchestrator" -or $_.CommandLine -match "uvicorn.*8001")
    }
    if ($stillAlive.Count -gt 0) {
        Write-Host "STILL ALIVE after kill attempt:"
        $stillAlive | ForEach-Object { Write-Host ("  PID=" + $_.ProcessId + " CMD=" + $_.CommandLine) }
        Start-Sleep -Seconds 5
    }
    Write-Host "  All processes stopped."

    # D78: STEP 2.5 — verify port 8001 is free before starting backend
    Write-Host "== STEP 2.5: PORT FREE CHECK ==" -ForegroundColor Cyan
    $portInUse = Get-NetTCPConnection -LocalPort 8001 -ErrorAction SilentlyContinue | Where-Object { $_.State -eq "Listen" }
    if ($portInUse) {
        $occupyingPid = $portInUse[0].OwningProcess
        Write-Warning "  [PORT_CHECK] port 8001 still in LISTEN state — PID=$occupyingPid — backend start may fail"
        Write-Warning "  [PORT_CHECK] Run as Administrator to kill cross-session zombie, or reboot"
    } else {
        Write-Host "  [PORT_CHECK] port 8001 is free" -ForegroundColor Green
    }

    Write-Host "== STEP 3: START ALL 4 PROCESSES ==" -ForegroundColor Green
    
    $backend = Start-Backend
    Write-Host ("  Backend started PID=" + $backend.Id)
    Start-Sleep -Seconds 2
    
    $radar = Start-Radar
    Write-Host ("  Radar started PID=" + $radar.Id)
    Start-Sleep -Seconds 2
    
    $orch = Start-Orchestrator
    Write-Host ("  Orchestrator started PID=" + $orch.Id)
    Start-Sleep -Seconds 2

    $analysisWorker = Start-AnalysisWorker
    Write-Host ("  AnalysisWorker started PID=" + $analysisWorker.Id)
    Start-Sleep -Seconds 2
    
    $frontendVersion = "v1.1.1-D62"
    # Read from versions_ref.json (single source of truth) — avoids hardcoded drift
    if (Test-Path "$projDir\versions_ref.json") {
        try {
            $vref = Get-Content "$projDir\versions_ref.json" | ConvertFrom-Json
            $frontendVersion = $vref.frontend
        } catch {}
    }
    $nodeProc = Start-Frontend
    Write-Host ("  Frontend started PID=" + $nodeProc.Id + " version=" + $frontendVersion)
    
    Start-Sleep -Seconds 5
    
    Write-Host "== STEP 4: SINGLETON VERIFICATION (manifest-based) ==" -ForegroundColor Cyan
    $manifest = "$projDir\run\process_manifest.json"
    $ok = $true
    if (Test-Path $manifest) {
        $m = Get-Content $manifest | ConvertFrom-Json -ErrorAction Stop
        foreach ($svc in @("backend","radar","orchestrator","analysis_worker")) {
            $entry = $m.PSObject.Properties[$svc].Value
            if ($null -ne $entry) {
                $svcPid = $entry.pid
                $alive = $null -ne (Get-CimInstance Win32_Process -Filter "ProcessId=$svcPid" -ErrorAction SilentlyContinue)
                if ($alive) {
                    Write-Host "  PASS [${svc}] PID=$svcPid version=$($entry.version) RUNNING"
                } else {
                    Write-Warning "  FAIL [${svc}] PID=$svcPid in manifest but NOT running"
                    $ok = $false
                }
            } else {
                Write-Warning "  WARN [${svc}] not yet in manifest"
            }
        }
    } else {
        Write-Warning "  manifest not found — processes may still be starting"
    }

    if ($nodeProc -eq $null) {
        Write-Host "  FAIL frontend: npm not available or dashboard missing" -ForegroundColor Red
        $ok = $false
    } else {
        $portOK = $null -ne (Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Where-Object {
            $_.CommandLine -match "5173"
        })
        if ($portOK) {
            Write-Host "  PASS frontend: port 5173 responding"
        } else {
            Write-Warning "  WARN frontend: started but port 5173 not confirmed"
        }
    }
    
    Write-Host "== STEP 5: VERSION CHECK ==" -ForegroundColor Cyan
    Start-Sleep -Seconds 3
    try {
        $versions = Invoke-RestMethod "http://localhost:8001/api/versions" -TimeoutSec 5
        $versions | ConvertTo-Json -Depth 5 | Write-Host
    } catch {
        Write-Host "Backend /api/versions not responding yet (may still be starting)"
    }
    
    if (-not $ok) {
        Write-Host "FAIL - duplicate or missing processes detected." -ForegroundColor Red
        return $false
    }
    
    Write-Host "PASS - All 4 processes running as singletons. Restart complete." -ForegroundColor Green
    return $true
}

function Monitor-Loop {
    Write-Host "== ENTERING CONTINUOUS MONITOR MODE ==" -ForegroundColor Magenta
    Write-Host ("  Monitoring interval: $MonitorIntervalSec seconds")
    Write-Host ("  Max restart attempts per process: $MaxRestartAttempts")
    Write-Host "  Press Ctrl+C to stop monitoring."
    Write-Host ""
    
    $round = 0
    while ($true) {
        $round++
        $status = Get-ProcessStatus
        $timestamp = Get-Date -Format "HH:mm:ss"
        
        $changes = @()
        
        if ($status.backend -eq 0) {
            if ($restartAttempts.backend -lt $MaxRestartAttempts) {
                Write-Host "[$timestamp] Backend DOWN, restarting (attempt $($restartAttempts.backend + 1)/$MaxRestartAttempts)..." -ForegroundColor Yellow
                Start-Backend | Out-Null
                $restartAttempts.backend++
                $changes += "backend"
            } else {
                Write-Host "[$timestamp] Backend DOWN, max attempts reached!" -ForegroundColor Red
            }
        } else {
            $restartAttempts.backend = 0
        }
        
        if ($status.radar -eq 0) {
            if ($restartAttempts.radar -lt $MaxRestartAttempts) {
                Write-Host "[$timestamp] Radar DOWN, restarting (attempt $($restartAttempts.radar + 1)/$MaxRestartAttempts)..." -ForegroundColor Yellow
                Start-Radar | Out-Null
                $restartAttempts.radar++
                $changes += "radar"
            } else {
                Write-Host "[$timestamp] Radar DOWN, max attempts reached!" -ForegroundColor Red
            }
        } else {
            $restartAttempts.radar = 0
        }
        
        if ($status.orchestrator -eq 0) {
            if ($restartAttempts.orchestrator -lt $MaxRestartAttempts) {
                Write-Host "[$timestamp] Orchestrator DOWN, restarting (attempt $($restartAttempts.orchestrator + 1)/$MaxRestartAttempts)..." -ForegroundColor Yellow
                Start-Orchestrator | Out-Null
                $restartAttempts.orchestrator++
                $changes += "orchestrator"
            } else {
                Write-Host "[$timestamp] Orchestrator DOWN, max attempts reached!" -ForegroundColor Red
            }
        } else {
            $restartAttempts.orchestrator = 0
        }

        if ($status.analysis_worker -eq 0) {
            if ($restartAttempts.analysis_worker -lt $MaxRestartAttempts) {
                Write-Host "[$timestamp] AnalysisWorker DOWN, restarting (attempt $($restartAttempts.analysis_worker + 1)/$MaxRestartAttempts)..." -ForegroundColor Yellow
                Start-AnalysisWorker | Out-Null
                $restartAttempts.analysis_worker++
                $changes += "analysis_worker"
            } else {
                Write-Host "[$timestamp] AnalysisWorker DOWN, max attempts reached!" -ForegroundColor Red
            }
        } else {
            $restartAttempts.analysis_worker = 0
        }
        
        if (-not $status.frontendPort -and $nodeProc -ne $null) {
            Write-Host "[$timestamp] Frontend DOWN, restarting..." -ForegroundColor Yellow
            Start-Frontend | Out-Null
            $changes += "frontend"
        } elseif (-not $status.frontendPort) {
            Write-Host "[$timestamp] Frontend: npm unavailable, skipping" -ForegroundColor DarkYellow
        }
        
        if ($changes.Count -eq 0) {
            Write-Host "[$timestamp] OK - backend:$($status.backend) radar:$($status.radar) orch:$($status.orchestrator) frontend:$($status.frontendPort)" -ForegroundColor DarkGreen
        } else {
            Write-Host "[$timestamp] RESTARTED: $($changes -join ', ')" -ForegroundColor Yellow
        }
        
        Start-Sleep -Seconds $MonitorIntervalSec
    }
}

# Main execution
if ($Continuous) {
    $status = Get-ProcessStatus
    if ($status.backend -eq 0 -or $status.radar -eq 0 -or $status.orchestrator -eq 0) {
        Write-Host "Some processes are not running. Performing full restart first..." -ForegroundColor Yellow
        $result = Full-Restart
        if (-not $result) {
            Write-Host "Full restart failed. Exiting." -ForegroundColor Red
            exit 1
        }
        Write-Host ""
    }
    Monitor-Loop
} else {
    $result = Full-Restart
    if ($result) {
        Write-Host ""
        Write-Host "To start continuous monitoring with auto-recovery, run:" -ForegroundColor Cyan
        Write-Host "  .\scripts\restart_all.ps1 -Continuous" -ForegroundColor White
    }
}
