# ═══════════════════════════════════════════════════════════════
# Shell Agent — Windows 一键打包脚本
# 输出：ShellAgent-1.0.0-windows.zip（含 shellagent.exe + console.html）
#
# 用法（在项目根目录 PowerShell 中运行）：
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\install-windows.ps1
#
# 依赖：Python 3.9+（需在 PATH 中）
# ═══════════════════════════════════════════════════════════════

$ErrorActionPreference = "Stop"

$APP_NAME    = "ShellAgent"
$VERSION     = "1.0.0"
$ZIP_OUT     = "${APP_NAME}-${VERSION}-windows.zip"

function Step($n, $total, $msg) {
    Write-Host "`n" -NoNewline
    Write-Host "[$n/$total] " -ForegroundColor Green -NoNewline
    Write-Host $msg
}

function Die($msg) {
    Write-Host "❌  $msg" -ForegroundColor Red
    exit 1
}

function Ok($msg) {
    Write-Host "✓  $msg" -ForegroundColor Green
}

function Warn($msg) {
    Write-Host "⚠  $msg" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "═══════════════════════════════════════" -ForegroundColor Cyan
Write-Host "   Shell Agent — Windows 打包脚本"      -ForegroundColor Cyan
Write-Host "═══════════════════════════════════════" -ForegroundColor Cyan

# ── 1. 前置检查 ───────────────────────────────────────────────
Step 1 5 "前置检查"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Die "未找到 python，请先安装 Python 3.9+：https://python.org/downloads"
}
$pyVer = python -c "import sys; print(sys.version_info[:2] >= (3,9))"
if ($pyVer -ne "True") { Die "需要 Python 3.9+，当前版本过低" }
Ok "Python $(python --version)"

if (-not (Test-Path "agent.py"))     { Die "未找到 agent.py，请在项目根目录运行" }
if (-not (Test-Path "console.html")) { Warn "未找到 console.html，跳过 Web 控制台" }

# ── 2. 安装依赖 ───────────────────────────────────────────────
Step 2 5 "安装打包依赖"

python -m pip install -q pyinstaller fastapi uvicorn
Ok "依赖安装完成"

# ── 3. PyInstaller 编译 ───────────────────────────────────────
Step 3 5 "PyInstaller 编译 agent.py → shellagent.exe"

# 清理旧构建
foreach ($d in @("build", "dist", "__pycache__")) {
    if (Test-Path $d) { Remove-Item -Recurse -Force $d }
}
Get-ChildItem -Filter "*.spec" | Remove-Item -Force

$hidden = @(
    "uvicorn.logging",
    "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on"
)
$args_list = @("--noconfirm", "--onefile", "--name", "shellagent", "--console")
foreach ($h in $hidden) {
    $args_list += "--hidden-import"
    $args_list += $h
}
if (Test-Path "console.html") {
    $args_list += "--add-data"
    $args_list += "console.html;."   # Windows 用分号
}
$args_list += "agent.py"

& pyinstaller @args_list
if (-not (Test-Path "dist\shellagent.exe")) { Die "编译失败，未找到 dist\shellagent.exe" }

$sizeMB = [math]::Round((Get-Item "dist\shellagent.exe").Length / 1MB, 1)
Ok "shellagent.exe 大小：${sizeMB} MB"

# ── 4. 生成 Windows 服务注册脚本 ─────────────────────────────
Step 4 5 "生成 Windows 服务脚本"

# 创建暂存目录
$STAGE = "dist\shellagent-windows"
if (Test-Path $STAGE) { Remove-Item -Recurse -Force $STAGE }
New-Item -ItemType Directory -Path $STAGE | Out-Null

# 复制主程序
Copy-Item "dist\shellagent.exe" "$STAGE\shellagent.exe"
if (Test-Path "console.html") {
    Copy-Item "console.html" "$STAGE\console.html"
}

# install-service.ps1：注册 Windows 服务，开机自启
@'
# Shell Agent — Windows 服务安装脚本
# 以管理员身份运行 PowerShell，然后执行：
#   .\install-service.ps1
#
# 或者直接双击 install-service.bat

$ErrorActionPreference = "Stop"
$EXE     = "$PSScriptRoot\shellagent.exe"
$SVC     = "ShellAgent"
$TOKEN   = $env:AGENT_TOKEN
$PORT    = $env:AGENT_PORT

# 如果未设置环境变量，弹窗询问
if (-not $TOKEN) {
    $TOKEN = Read-Host "请输入访问 Token（至少 6 位，直接回车使用默认值 my-secret-token）"
    if (-not $TOKEN) { $TOKEN = "my-secret-token" }
}
if (-not $PORT) {
    $PORT = Read-Host "请输入监听端口（直接回车使用默认值 8000）"
    if (-not $PORT) { $PORT = "8000" }
}

Write-Host "→ Token: $TOKEN"
Write-Host "→ Port:  $PORT"

# 停止并删除旧服务
if (Get-Service -Name $SVC -ErrorAction SilentlyContinue) {
    Write-Host "→ 停止旧服务..."
    Stop-Service -Name $SVC -Force -ErrorAction SilentlyContinue
    sc.exe delete $SVC | Out-Null
    Start-Sleep 2
}

# 设置系统环境变量（服务进程继承）
[System.Environment]::SetEnvironmentVariable("AGENT_TOKEN", $TOKEN, "Machine")
[System.Environment]::SetEnvironmentVariable("AGENT_PORT",  $PORT,  "Machine")
[System.Environment]::SetEnvironmentVariable("AGENT_HOST",  "0.0.0.0", "Machine")

# 注册为 Windows 服务（开机自启）
New-Service -Name $SVC `
    -BinaryPathName "`"$EXE`"" `
    -DisplayName "Shell Agent" `
    -Description "Shell Agent HTTP 服务，提供本地 Shell 远程执行能力" `
    -StartupType Automatic

# 立即启动
Start-Service -Name $SVC
Start-Sleep 2

$svc = Get-Service -Name $SVC
if ($svc.Status -eq "Running") {
    Write-Host "✅  Shell Agent 已启动，访问 http://localhost:$PORT" -ForegroundColor Green
} else {
    Write-Host "⚠  服务状态：$($svc.Status)，请检查日志" -ForegroundColor Yellow
    Write-Host "   查看日志：Get-EventLog -LogName Application -Source ShellAgent -Newest 10"
}
'@ | Set-Content "$STAGE\install-service.ps1" -Encoding UTF8

# install-service.bat：双击即可（自动请求管理员权限）
@'
@echo off
:: 请求管理员权限
net session >nul 2>&1
if %errorLevel% neq 0 (
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)
powershell -ExecutionPolicy Bypass -File "%~dp0install-service.ps1"
pause
'@ | Set-Content "$STAGE\install-service.bat" -Encoding ASCII

# uninstall-service.ps1
@'
$SVC = "ShellAgent"
if (Get-Service -Name $SVC -ErrorAction SilentlyContinue) {
    Stop-Service -Name $SVC -Force -ErrorAction SilentlyContinue
    sc.exe delete $SVC | Out-Null
    Write-Host "✅  Shell Agent 服务已卸载" -ForegroundColor Green
} else {
    Write-Host "服务不存在，无需卸载"
}
foreach ($key in @("AGENT_TOKEN","AGENT_PORT","AGENT_HOST")) {
    [System.Environment]::SetEnvironmentVariable($key, $null, "Machine")
}
'@ | Set-Content "$STAGE\uninstall-service.ps1" -Encoding UTF8

# README.txt
@'
Shell Agent for Windows
═══════════════════════

安装步骤：
  1. 双击 install-service.bat
  2. 弹出管理员权限确认 → 点「是」
  3. 输入 Token 和端口（直接回车用默认值）
  4. 看到「✅ Shell Agent 已启动」即完成

访问：
  浏览器打开 http://localhost:8000
  或直接打开 console.html

卸载：
  以管理员身份运行 PowerShell，执行：
  .\uninstall-service.ps1

修改配置：
  控制面板 → 系统 → 高级系统设置 → 环境变量
  修改 AGENT_TOKEN / AGENT_PORT 后重启服务：
  Restart-Service ShellAgent
'@ | Set-Content "$STAGE\README.txt" -Encoding UTF8

Ok "服务脚本生成完毕"

# ── 5. 打包成 ZIP ─────────────────────────────────────────────
Step 5 5 "打包成 $ZIP_OUT"

if (Test-Path $ZIP_OUT) { Remove-Item $ZIP_OUT -Force }
Compress-Archive -Path "$STAGE\*" -DestinationPath $ZIP_OUT

# 清理
Remove-Item -Recurse -Force $STAGE
foreach ($d in @("build", "dist", "__pycache__")) {
    if (Test-Path $d) { Remove-Item -Recurse -Force $d }
}
Get-ChildItem -Filter "*.spec" | Remove-Item -Force

$zipMB = [math]::Round((Get-Item $ZIP_OUT).Length / 1MB, 1)

Write-Host ""
Write-Host "═══════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "✅  打包完成！" -ForegroundColor Green
Write-Host "═══════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "   $ZIP_OUT  (${zipMB} MB)"
Write-Host ""
Write-Host "📦  发给 Windows 用户后："
Write-Host "    1. 解压 ZIP"
Write-Host "    2. 双击 install-service.bat"
Write-Host "    3. 输入 Token 和端口 → 完成"
Write-Host "═══════════════════════════════════════════════════" -ForegroundColor Cyan
