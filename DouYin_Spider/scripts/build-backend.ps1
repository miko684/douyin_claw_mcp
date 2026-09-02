$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $projectRoot
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw '未找到 Python，请先安装 Python 3.10+。' }
python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host '正在安装 PyInstaller…'; python -m pip install pyinstaller }
$buildRoot = Join-Path $projectRoot 'work\pyinstaller'
$distRoot = Join-Path $projectRoot 'release\pyinstaller'
$backendRoot = Join-Path $projectRoot 'release\backend'
if (Test-Path $backendRoot) { Remove-Item -LiteralPath $backendRoot -Recurse -Force }
New-Item -ItemType Directory -Force -Path $backendRoot | Out-Null
python -m PyInstaller --noconfirm --clean --onedir --name electron_bridge `
  --paths $projectRoot --hidden-import static.Response_pb2 --hidden-import static.Request_pb2 `
  --distpath $distRoot --workpath $buildRoot --specpath $buildRoot (Join-Path $projectRoot 'electron_bridge.py')
Copy-Item -Path (Join-Path $distRoot 'electron_bridge\*') -Destination $backendRoot -Recurse -Force
Write-Host "后端已生成：$backendRoot"
