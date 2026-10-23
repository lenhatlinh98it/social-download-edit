@echo off
rem Start Grabber without a console window (run setup.bat once first).
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Grabber is not set up yet. Run setup.bat first.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0app.py"
