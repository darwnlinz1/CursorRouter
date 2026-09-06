@echo off
setlocal
title CursorRouter - Desktop App Builder
echo =========================================================
echo   Building CursorRouter Standalone Windows App (WebView2)
echo =========================================================
cd /d "%~dp0"
python -m pip install -r requirements.txt
python build.py %*
echo.
echo Build finished. Binary located in: %~dp0dist\
pause
