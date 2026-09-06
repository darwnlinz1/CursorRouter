@echo off
setlocal
title CursorRouter Desktop App
cd /d "%~dp0"
python desktop_main.py %*
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Launch failed. Installing requirements...
    python -m pip install -r requirements.txt
    python desktop_main.py %*
)
pause
