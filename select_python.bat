@echo off
set "PYTHON_EXE="
set "DOUYIN_PORTABLE_PYTHON=0"

if exist "%~dp0.venv\Scripts\python.exe" (
  set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
  exit /b 0
)

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
  for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
)
if defined PYTHON_EXE exit /b 0

if exist "%~dp0.runtime\python.exe" (
  set "PYTHON_EXE=%~dp0.runtime\python.exe"
  set "DOUYIN_PORTABLE_PYTHON=1"
  exit /b 0
)

exit /b 1
