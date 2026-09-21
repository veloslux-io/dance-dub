@echo off
REM Dance Dub one-time installer: creates venv + installs dependencies.
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo Python's "py" launcher not found.
    echo Install Python from https://www.python.org/downloads/
    echo and CHECK "Add python.exe to PATH" on the first screen, then re-run this file.
    pause
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3 -m venv venv
    if errorlevel 1 (
        echo Failed to create venv. See message above.
        pause
        exit /b 1
    )
)

echo Installing packages (this can take a few minutes)...
"venv\Scripts\python.exe" -m pip install --upgrade pip >nul
"venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Installation failed. Check your internet connection and re-run.
    pause
    exit /b 1
)

echo.
echo Done! Double-click run_gui.bat to start Dance Dub.
pause
