@echo off
chcp 65001 >nul
title Cursor Manager - Native Desktop App
cd /d "%~dp0"

cls
echo ======================================================================
echo   CURSOR MANAGER - NATIVE DESKTOP APP (WEBVIEW2)
echo ======================================================================
echo   Cua so Native Desktop doc lap (Microsoft Edge WebView2)
echo   Tu dong kiem tra tien trinh Cursor IDE va Dong bo hai chieu
echo   Rotating Proxy: Port 8999 (Connect-RPC Auto-Swap)
echo   Watcher chu ky 5s / Two-Tier Quota / Blacklist 7 ngay
echo ======================================================================
echo.

set "PY_CMD="

if exist "%USERPROFILE%\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe" (
    set "PY_CMD=%USERPROFILE%\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
) else if exist "%USERPROFILE%\AppData\Local\Programs\Python\Python312\python.exe" (
    set "PY_CMD=%USERPROFILE%\AppData\Local\Programs\Python\Python312\python.exe"
) else if exist "%USERPROFILE%\AppData\Local\Programs\Python\Python311\python.exe" (
    set "PY_CMD=%USERPROFILE%\AppData\Local\Programs\Python\Python311\python.exe"
) else (
    where python >nul 2>&1
    if not errorlevel 1 (
        set "PY_CMD=python"
    )
)

if "%PY_CMD%"=="" (
    echo [!] Khong tim thay Python hop le! Vui long kiem tra PATH.
    pause
    exit /b 1
)

echo [*] Dang khoi chay Native Desktop Window...
"%PY_CMD%" native_app.py

if errorlevel 1 (
    echo.
    echo [-] Ung dung bi dung hoac gap loi.
    pause
)
