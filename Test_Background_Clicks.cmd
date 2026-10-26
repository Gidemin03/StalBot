@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv-benchmark\Scripts\python.exe" (
  echo Python environment not found: .venv-benchmark\Scripts\python.exe
  pause
  exit /b 1
)
".venv-benchmark\Scripts\python.exe" -X utf8 tools\background_click_probe.py %*
pause
