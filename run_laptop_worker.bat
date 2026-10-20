@echo off
title Voice Clone Pro - Laptop Applio GPU Worker
color 0B
echo ========================================================
echo  Voice Clone Pro - Dedicated Applio Local Worker
echo  Running on Local Machine with C:\Applio
echo ========================================================
echo.

cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    echo [INFO] Starting worker using project virtualenv...
    ".venv\Scripts\python.exe" laptop_applio_worker.py
) else (
    echo [INFO] Starting worker using system Python...
    python laptop_applio_worker.py
)

pause
