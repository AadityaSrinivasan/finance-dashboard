@echo off
REM Starts the finance dashboard at http://127.0.0.1:8765 (this computer only).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up Python environment. This downloads the libraries once...
  py -3.13 -m venv .venv || python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
)
".venv\Scripts\python.exe" -m app
pause
