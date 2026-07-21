@echo off
:: Change directory to where the script is located
cd /d "%~dp0"

:: If this script was called with an argument by itself, route to the correct loop
if "%1"=="SERVER" goto run_server
if "%1"=="WORKER" goto run_worker

:: ==========================================
:: MAIN LAUNCHER WINDOW
:: ==========================================
color 0A
echo ========================================
echo   STARTING RADIO COMMAND CENTER
echo ========================================
echo.
echo Launching Server and Worker in separate windows...
echo (To completely shut down the system, close all three windows)
echo.

:: Start the sub-processes in separate windows by calling this script with arguments
start "Radio Command - Server" cmd /c "%~f0" SERVER
start "Radio Command - AI Worker" cmd /c "%~f0" WORKER

echo [SYSTEM] All services launched! 
echo [SYSTEM] Web Interface is available at: http://localhost:8000
echo.
pause
exit /b

:: ==========================================
:: SERVER PROCESS LOOP
:: ==========================================
:run_server
:: Light Yellow Text for Server Logs
color 0E
:server_loop
echo [SYSTEM] Starting Web Server on port 8000...
venv\Scripts\uvicorn.exe server:app --host 0.0.0.0 --port 8000
echo.
echo [SYSTEM] WARNING: Web Server crashed or stopped! Restarting in 3 seconds...
timeout /t 3 /nobreak >nul
goto server_loop

:: ==========================================
:: WORKER PROCESS LOOP
:: ==========================================
:run_worker
:: Light Purple Text for Worker Logs
color 0D
:worker_loop
echo [SYSTEM] Starting AI Transcription Worker...
venv\Scripts\python.exe worker.py
echo.
echo [SYSTEM] WARNING: Worker crashed or stopped! Restarting in 3 seconds...
timeout /t 3 /nobreak >nul
goto worker_loop