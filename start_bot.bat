@echo off
title Voice Clone Pro - Production Service
cd /d "%~dp0"

echo ===================================================
echo       Voice Clone Pro - Production Service
echo ===================================================
echo Starting Active Polling (Bot + Background Audio Worker)...
echo.

"C:\Users\awint\AppData\Local\Programs\Python\Python310\python.exe" main.py --mode all

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Bot stopped with an error code: %ERRORLEVEL%
    pause
)
