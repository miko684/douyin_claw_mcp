@echo off
cd /d "%~dp0"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0.browser"
set "PYTHONPATH=%~dp0;%~dp0DouYin_Spider"
set "DOUYIN_PROJECT_ROOT=%~dp0DouYin_Spider"
set "DOUYIN_STATS_FILE=%~dp0work\mcp_stats.json"
if not exist "deps-ready.txt" call "%~dp0install.bat"
if not exist "deps-ready.txt" exit /b 1
call "%~dp0select_python.bat"
if errorlevel 1 exit /b 1

"%PYTHON_EXE%" -c "import requests" >nul 2>&1
if errorlevel 1 (
  echo 当前 Python 缺少运行依赖，正在自动安装... 1>&2
  set "DOUYIN_NONINTERACTIVE=1"
  call "%~dp0install.bat" 1>&2
  if errorlevel 1 exit /b 1
  call "%~dp0select_python.bat"
)

"%PYTHON_EXE%" "%~dp0run_local.py" dashboard_server.py
