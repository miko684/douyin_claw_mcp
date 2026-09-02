@echo off
setlocal
cd /d "%~dp0"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0.browser"
set "PYTHONPATH=%~dp0;%~dp0DouYin_Spider"

if not exist "deps-ready.txt" (
  echo 尚未安装依赖，正在打开安装程序... 1>&2
  set "DOUYIN_NONINTERACTIVE=1"
  call "%~dp0install.bat" 1>&2
  if not exist "deps-ready.txt" exit /b 1
)

call "%~dp0select_python.bat"
if errorlevel 1 exit /b 1

"%PYTHON_EXE%" -c "import mcp, playwright, requests" >nul 2>&1
if errorlevel 1 (
  echo 当前 Python 缺少 MCP 依赖，正在自动安装... 1>&2
  set "DOUYIN_NONINTERACTIVE=1"
  call "%~dp0install.bat" 1>&2
  if errorlevel 1 exit /b 1
  call "%~dp0select_python.bat"
)

set "DOUYIN_PROJECT_ROOT=%~dp0DouYin_Spider"
set "DOUYIN_STATS_FILE=%~dp0work\mcp_stats.json"
if not defined DOUYIN_OPEN_DASHBOARD set "DOUYIN_OPEN_DASHBOARD=0"
echo MCP 启动中，网页控制台已在本机启动；请使用 Codex 内置浏览器访问 127.0.0.1:8765。 1>&2
"%PYTHON_EXE%" "%~dp0run_local.py" mcp_server.py
