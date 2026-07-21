@echo off
:: Move back to the project root directory
cd /d "%~dp0\.."

echo ========================================
echo   RADIO COMMAND CENTER - INSTALLER (WIN)
echo ========================================

echo [1/4] Creating data directories...
if not exist "data\live_audio" mkdir "data\live_audio"

echo [2/4] Creating Python Virtual Environment...
python -m venv venv

echo [3/4] Installing dependencies (this may take a minute)...
venv\Scripts\python.exe -m pip install --upgrade pip
venv\Scripts\pip.exe install -r requirements.txt

echo ========================================
echo  INSTALLATION COMPLETE!
echo  You can now close this window and double-click 'start.bat'
echo ========================================
pause