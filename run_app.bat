@echo off
title MediGuard AI Launcher
chcp 65001 > nul
echo ======================================================
echo           DANG KHOI DONG MEDIGUARD AI
echo ======================================================

set PY_CMD=python
if exist "C:\Users\Admin\anaconda3\python.exe" (
    set PY_CMD=C:\Users\Admin\anaconda3\python.exe
)

echo [1/2] Dang bat Backend FastAPI (Port 8000)...
start "MediGuard Backend" cmd /k "chcp 65001 > nul && cd /d %~dp0backend && set PYTHONIOENCODING=utf-8 && set PYTHONUTF8=1 && %PY_CMD% -X utf8 main.py"

timeout /t 3 /nobreak > nul

echo [2/2] Dang bat Frontend React Kiosk (Port 5173)...
start "MediGuard Frontend" cmd /k "chcp 65001 > nul && cd /d %~dp0frontend && npm run dev"

timeout /t 2 /nobreak > nul

echo Mo trinh duyet...
start http://localhost:5173

echo ======================================================
echo     MEDIGUARD AI DA KHOI DONG THANH CONG!
echo ======================================================
pause
