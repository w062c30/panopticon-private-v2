# ============================================================
# Panopticon 一鍵啟動腳本 (PowerShell)
# ============================================================
# 功能：
#   1. 啟動 Shadow Hydration Pipeline（錢包發現 + 雷達）
#   2. 啟動 Backend API（FastAPI， port 8000）
#   3. 啟動 Frontend Dashboard（Vite， port 5173）
#
# 使用方式：
#   .\scripts\start_all.ps1
#
# 停止：Ctrl+C 或關閉終端窗口
# ============================================================

$ErrorActionPreference = "Continue"

Write-Host "[PANOPTICON] 啟動所有服務..." -ForegroundColor Cyan
Write-Host ""

# 檢查 .env 是否存在
if (-not (Test-Path ".env")) {
    Write-Host "[WARNING] .env 不存在，請先執行 scripts\unify_env.py" -ForegroundColor Yellow
}

# 啟動 Shadow Hydration Pipeline
Write-Host "[1/3] 啟動 Shadow Hydration Pipeline..." -ForegroundColor Yellow
Start-Process -FilePath "python" -ArgumentList "scripts\start_shadow_hydration.py" -WindowStyle Normal -WorkingDirectory $PWD
Write-Host "      已啟動（查看新窗口）" -ForegroundColor Green
Start-Sleep -Seconds 2

# 啟動 Backend API
Write-Host "[2/3] 啟動 Backend API (http://127.0.0.1:8001)..." -ForegroundColor Yellow
Start-Process -FilePath "python" -ArgumentList "-m uvicorn panopticon_py.api.app:app --host 127.0.0.1 --port 8001 --reload" -WindowStyle Normal -WorkingDirectory $PWD
Write-Host "      API 已啟動" -ForegroundColor Green
Start-Sleep -Seconds 2

# 啟動 Frontend Dashboard
Write-Host "[3/3] 啟動 Frontend Dashboard (http://localhost:5173)..." -ForegroundColor Yellow
Start-Process -FilePath "npm" -ArgumentList "run dev" -WindowStyle Normal -WorkingDirectory "$PWD\dashboard"
Write-Host "      Frontend 已啟動" -ForegroundColor Green

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "[完成] 所有服務已啟動" -ForegroundColor Cyan
Write-Host ""
Write-Host "   - Shadow Hydration: 查看新啟動的窗口" -ForegroundColor White
Write-Host "   - Backend API:      http://127.0.0.1:8001" -ForegroundColor White
Write-Host "   - API Docs:         http://127.0.0.1:8001/docs" -ForegroundColor White
Write-Host "   - Frontend:         http://localhost:5173" -ForegroundColor White
Write-Host ""
Write-Host "   Insight 報告（每2小時）: powershell -File scripts\schedule_insight_report.ps1" -ForegroundColor White
Write-Host "============================================================" -ForegroundColor Cyan