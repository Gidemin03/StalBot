@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv-benchmark\Scripts\python.exe" (
  echo OCR environment not found: .venv-benchmark\Scripts\python.exe
  pause
  exit /b 1
)
".venv-benchmark\Scripts\python.exe" main.py
if errorlevel 1 pause
