@echo off
setlocal
cd /d "%~dp0"
python --version
if errorlevel 1 (
    echo Install Python 3.10 or newer from python.org and enable Add Python to PATH.
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install --no-cache-dir -r requirements.txt
if errorlevel 1 goto failed
echo Setup complete. Open Launch Duplicate Cleaner.bat to start the app.
pause
exit /b 0
:failed
echo Setup failed. Check the error above. No scanned files were changed.
pause
exit /b 1
