@echo off
title Voice Clone Pro - High-Speed Laptop Local Worker
color 0B
echo ===================================================================
echo  🎙️ VOICE CLONE PRO - HIGH-SPEED LOCAL WORKER (GPU / CUDA)
echo  Connected to Render Cloud Database with Real-time Live Queue
echo ===================================================================
echo.
echo Engine Modes:
echo   [1] ALL Engines (Local Applio GPU + Cloud VoxCPM AI) - [DEFAULT]
echo   [2] Applio Local GPU Only (engine='applio')
echo   [3] VoxCPM Cloud AI Only (engine='voxcpm')
echo.
echo Starting in ALL-ENGINE mode in 3 seconds... (Press 2 or 3 to change)
choice /C 123 /N /T 3 /D 1 /M "Select mode (1/2/3): "
set SELECTED_OPT=%ERRORLEVEL%

set ENGINE_ARG=all
if "%SELECTED_OPT%"=="2" set ENGINE_ARG=applio
if "%SELECTED_OPT%"=="3" set ENGINE_ARG=voxcpm

echo.
echo [INFO] Selected Engine Mode: %ENGINE_ARG%
echo [INFO] Starting worker process...
echo.

cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" laptop_applio_worker.py --engine %ENGINE_ARG%
) else (
    python laptop_applio_worker.py --engine %ENGINE_ARG%
)

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Worker stopped with exit code %ERRORLEVEL%.
)
pause
