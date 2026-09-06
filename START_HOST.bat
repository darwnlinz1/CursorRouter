@echo off
chcp 65001 >nul
title Cursor Manager - Auto-Rotation & Quota Host (Port 7860)
cd /d "%~dp0"

cls
echo ======================================================================
echo   CURSOR MANAGER - AUTO-ROTATION AND QUOTA DASHBOARD
echo ======================================================================
echo   Web Dashboard: http://127.0.0.1:7860 (hoac http://localhost:7860)
echo   Rotating Proxy: Port 8999 (Connect-RPC Auto-Swap)
echo   Auto-Rotate: Active (Giam sat chu ky 5s / Nguong khoa 50.0%%)
echo.
echo [*] Dang khoi dong Cursor Manager Host tu main.py...
echo [*] Trinh duyet se tu dong mo http://127.0.0.1:7860 sau giay lat...
echo.
echo ======================================================================
echo.

set "PY_CMD="

if exist "%USERPROFILE%\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe" (
    set "PY_CMD=%USERPROFILE%\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
) else if exist "%USERPROFILE%\AppData\Local\Programs\Python\Python312\python.exe" (
    set "PY_CMD=%USERPROFILE%\AppData\Local\Programs\Python\Python312\python.exe"
) else (
    where python >nul 2>&1
    if not errorlevel 1 (
        set "PY_CMD=python"
    )
)

if "%PY_CMD%"=="" (
    echo [!] Khong tim thay Python hop le! Vui long cai dat Python hoac them vao PATH.
    pause
    exit /b 1
)

start "" cmd /c "timeout /t 1 /nobreak >nul & start http://127.0.0.1:7860"

"%PY_CMD%" main.py

if errorlevel 1 (
    echo.
    echo [-] Host bi dung dot ngot hoac gap loi.
    echo.
    pause
)
