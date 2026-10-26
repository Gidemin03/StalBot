@echo off
setlocal
cd /d "%~dp0"
set "BUILDER_PYTHON=%~dp0..\.venv\Scripts\python.exe"
if not exist "%BUILDER_PYTHON%" (
  echo Project Python environment not found: "%BUILDER_PYTHON%"
  echo Create it from the project directory with: py -3 -m venv .venv
  pause
  exit /b 1
)
"%BUILDER_PYTHON%" -c "from PIL import Image, ImageTk; import tkinter" >nul 2>nul
if errorlevel 1 (
  echo Project environment cannot load Pillow or Tkinter.
  echo Repair dependencies with:
  echo "%BUILDER_PYTHON%" -m pip install -r "%~dp0requirements-builder.txt"
  echo Tkinter requires a Python installation with Tcl/Tk enabled.
  pause
  exit /b 1
)
"%BUILDER_PYTHON%" "%~dp0builder.py" %*
set "BUILDER_EXIT=%ERRORLEVEL%"
if not "%BUILDER_EXIT%"=="0" pause
exit /b %BUILDER_EXIT%
