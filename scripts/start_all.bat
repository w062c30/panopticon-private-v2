@echo off
REM ============================================================
REM Panopticon 一鍵啟動腳本
REM ============================================================
REM 功能：
REM   1. 啟動 Shadow Hydration Pipeline（錢包發現 + 雷達）
REM   2. 啟動 Backend API（FastAPI， port 8001）
REM   3. 啟動 Frontend Dashboard（Vite， port 5173）
REM
REM 使用方式：
REM   scripts\start_all.bat
REM
REM 停止：Ctrl+C 或關閉終端窗口
REM ============================================================

echo [PANOPTICON] 啟動所有服務...
echo.

REM 檢查 .env 是否存在
if not exist ".env" (
    echo [WARNING] .env 不存在，請先執行 scripts\unify_env.py
)

REM 啟動 Shadow Hydration Pipeline（背景運行）
echo [1/3] 啟動 Shadow Hydration Pipeline...
start "Panopticon-Shadow" cmd /c "python scripts\start_shadow_hydration.py"
echo      已在新窗口啟動，請勿關閉該窗口
echo.

REM 等待 2 秒
timeout /t 2 /nobreak >nul

REM 啟動 Backend API
echo [2/3] 啟動 Backend API (http://127.0.0.1:8001)...
start "Panopticon-API" cmd /c "python -m uvicorn panopticon_py.api.app:app --host 127.0.0.1 --port 8001 --reload"
echo      API 已啟動
echo.

REM 啟動 Frontend Dashboard
echo [3/3] 啟動 Frontend Dashboard (http://localhost:5173)...
cd dashboard
start "Panopticon-Frontend" cmd /c "npm run dev"
cd ..

echo.
echo ============================================================
echo [完成] 所有服務已啟動
echo.
echo   - Shadow Hydration: 查看 "Panopticon-Shadow" 窗口
echo   - Backend API:      http://127.0.0.1:8001
echo   - API Docs:         http://127.0.0.1:8001/docs
echo   - Frontend:         http://localhost:5173
echo.
echo   Insight 報告（每2小時）: scripts\schedule_insight_report.ps1
echo ============================================================
pause