@echo off
setlocal
cd /d "%~dp0"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0.browser"

call "%~dp0select_python.bat"
if errorlevel 1 (
  echo 找不到可用的 Python，也找不到内置运行时。
  echo 请确认 .runtime 文件夹仍在本目录中。
  if not defined DOUYIN_NONINTERACTIVE pause
  exit /b 1
)

if "%DOUYIN_PORTABLE_PYTHON%"=="1" (
  echo 未检测到系统 Python，正在使用本目录内置 Python。
) else (
  if not exist ".venv\Scripts\python.exe" (
    echo 检测到系统 Python，正在创建本地运行环境...
    "%PYTHON_EXE%" -m venv .venv
    if errorlevel 1 (
      echo 创建本地运行环境失败。
      if not defined DOUYIN_NONINTERACTIVE pause
      exit /b 1
    )
  )
  call "%~dp0select_python.bat"
)

echo 正在安装 MCP 和抖音采集依赖...
"%PYTHON_EXE%" -m pip install --upgrade pip
"%PYTHON_EXE%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo 依赖安装失败，请检查网络后重新运行本文件。
  if not defined DOUYIN_NONINTERACTIVE pause
  exit /b 1
)

echo 正在安装浏览器运行组件...
"%PYTHON_EXE%" -m playwright install chromium
if errorlevel 1 (
  echo Chromium 安装失败，请检查网络后重新运行本文件。
  if not defined DOUYIN_NONINTERACTIVE pause
  exit /b 1
)

>"%~dp0deps-ready.txt" echo ready
echo 安装完成。下一步可以双击 start_mcp.bat。
if not defined DOUYIN_NONINTERACTIVE pause
