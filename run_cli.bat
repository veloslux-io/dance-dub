@echo off
REM Dance Dub CLI launcher
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"

if not exist "venv\Scripts\python.exe" (
    echo Not installed yet. Run install.bat first.
    pause
    exit /b 1
)
"venv\Scripts\python.exe" -m dance_dub.cli %*
