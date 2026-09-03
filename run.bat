@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo LegiView virtual environment was not found. Run: py -m venv .venv
  exit /b 1
)
".venv\Scripts\python.exe" -m olis_archive serve
endlocal
