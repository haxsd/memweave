@echo off
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Project Python environment missing. See README.md for installation.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -m memweave ui --home "%~dp0work\runtime\memory home" --port 8765
if errorlevel 1 pause
