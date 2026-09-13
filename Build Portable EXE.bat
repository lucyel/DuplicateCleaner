@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run Setup.bat first to install the build environment.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install --no-cache-dir -r requirements-build.txt
if errorlevel 1 goto failed
set "PYINSTALLER_CONFIG_DIR=%CD%\build\pyinstaller-cache"
rem Do not bundle unrelated runtime DLLs from development tools on PATH.
set "PATH=%SystemRoot%\System32;%SystemRoot%"
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed --noupx --name DuplicateCleaner --distpath dist --workpath build --specpath build --hidden-import win32timezone run.py
if errorlevel 1 goto failed
echo Build complete: dist\DuplicateCleaner.exe
echo Copy that single file to another Windows computer. Python is not required there.
pause
exit /b 0
:failed
echo Build failed. Check the error above. No scanned files were changed.
pause
exit /b 1
