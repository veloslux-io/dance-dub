@echo off
REM Dance Dub GUI launcher (double-click me)
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"

if not exist "venv\Scripts\pythonw.exe" (
    echo Not installed yet. Run install.bat first.
    pause
    exit /b 1
)
"venv\Scripts\pythonw.exe" -m dance_dub.gui
if errorlevel 1 (
    echo Something went wrong. Try run_cli.bat --help to see the error.
    pause
)
