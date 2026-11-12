@echo off
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Project Python environment missing. See README.md for installation.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -m memweave ui --home "%USERPROFILE%\.memweave" --port 8765 --root "%~dp0."
if errorlevel 1 pause
