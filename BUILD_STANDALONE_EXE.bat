@echo off
setlocal
title Cursor Manager - Standalone Windows Executable Builder
chcp 65001 >nul

echo =====================================================================
echo   🚀 CURSOR MANAGER - STANDALONE WINDOWS BUILDER
echo =====================================================================
echo.

cd /d "%~dp0"

echo [*] Kiem tra moi truong Python...
python --version >nul 2>&1
if "%ERRORLEVEL%" NEQ "0" (
    echo [-] Khong tim thay Python trong PATH! Vui long cai dat Python 3.10+
    pause
    exit /b 1
)

echo [*] Kiem tra va cai dat PyInstaller neu can...
python -c "import PyInstaller" >nul 2>&1
if "%ERRORLEVEL%" NEQ "0" (
    echo [*] Dang cai dat PyInstaller...
    python -m pip install pyinstaller
)

echo [*] Bat dau qua trinh dong goi standalone CursorManager.exe...
python build_standalone_exe.py %*

if "%ERRORLEVEL%"=="0" (
    echo.
    echo =====================================================================
    echo   ✅ BUILD THANH CONG! File .exe nam tai thu muc: dist\CursorManager.exe
    echo =====================================================================
) else (
    echo.
    echo [-] Qua trinh build gap loi. Vui long kiem tra log o tren.
)

pause
exit /b %ERRORLEVEL%
