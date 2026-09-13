@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Run Setup.bat first to install Duplicate Cleaner.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -c "import PySide6, win32file, pythoncom" >nul 2>&1
if errorlevel 1 (
    echo Dependencies are missing. Run Setup.bat first.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0run.py"
