@echo off
rem Seller only: make a license key for a buyer's Machine ID (copied to the clipboard).
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set /p MID=Machine ID from the buyer:
set /p NOTE=Customer / order note (optional):
for /f "delims=" %%K in ('"%PY%" tools\keygen.py issue "%MID%" --note "%NOTE%"') do set "KEY=%%K"
if not defined KEY (
  echo Failed - check the Machine ID.
  pause
  exit /b 1
)
echo.
echo %KEY%
echo %KEY%| clip
echo.
echo Key copied to the clipboard. All keys are listed in %USERPROFILE%\.grabber-keygen\issued.csv
pause
