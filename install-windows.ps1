# ═══════════════════════════════════════════════════════════════
# Shell Agent — Windows 一键打包脚本
# 输出：ShellAgent-1.0.0-windows.zip
#   - shellagent.exe      (后台服务)
#   - ShellAgentTray.exe  (系统托盘应用)
#   - console.html        (Web 控制台)
#   - 安装/卸载脚本
#
# 用法（在项目根目录 PowerShell 中运行）：
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\install-windows.ps1
#
# 依赖：
#   - Python 3.9+
#   - pip install pyinstaller pystray pillow
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
    Write-Host "[ERROR] $msg" -ForegroundColor Red
    exit 1
}

function Ok($msg) {
    Write-Host "[OK] $msg" -ForegroundColor Cyan
}

function Warn($msg) {
    Write-Host "[WARN] $msg" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "   Shell Agent — Windows 打包脚本 v$VERSION"      -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan

# ══════════════════════════════════════════════════════════════
# 1. 前置检查
# ══════════════════════════════════════════════════════════════
Step 1 6 "前置检查"

# 检查 Python
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Die "未找到 python，请先安装 Python 3.9+：https://python.org/downloads"
}
$pyVer = python -c "import sys; print(sys.version_info[:2] >= (3,9))"
if ($pyVer -ne "True") { Die "需要 Python 3.9+，当前版本过低" }
Ok "Python $(python --version)"

# 检查必要文件
if (-not (Test-Path "agent.py"))     { Die "未找到 agent.py，请在项目根目录运行" }
if (-not (Test-Path "config.py"))    { Die "未找到 config.py" }
if (-not (Test-Path "console.html")) { Warn "未找到 console.html，跳过 Web 控制台" }

# 检查/生成 tray_app_windows.py
if (-not (Test-Path "tray_app_windows.py")) {
    Die "未找到 tray_app_windows.py，请确保文件存在"
}
Ok "所有必要文件已就绪"

# ══════════════════════════════════════════════════════════════
# 2. 安装打包依赖
# ══════════════════════════════════════════════════════════════
Step 2 6 "安装打包依赖"

python -m pip install -q pyinstaller fastapi uvicorn pystray pillow pika cryptography
Ok "依赖安装完成"

# ══════════════════════════════════════════════════════════════
# 3. 编译后台服务 agent.py
# ══════════════════════════════════════════════════════════════
Step 3 6 "编译后台服务 agent.py -> shellagent.exe"

# 清理旧构建
foreach ($d in @("build", "dist", "__pycache__")) {
    if (Test-Path $d) { Remove-Item -Recurse -Force $d }
}
Get-ChildItem -Filter "*.spec" | Remove-Item -Force -ErrorAction SilentlyContinue

# Hidden imports for uvicorn + pika + cryptography
$hidden = @(
    "uvicorn.logging",
    "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on",
    # MQ Consumer
    "pika", "pika.adapters", "pika.adapters.blocking_connection",
    # Cryptography
    "cryptography", "cryptography.hazmat", "cryptography.hazmat.primitives",
    "cryptography.hazmat.primitives.ciphers", "cryptography.hazmat.backends",
    # Local modules
    "mq_consumer", "config"
)

$args_list = @("--noconfirm", "--onefile", "--name", "shellagent", "--console")
foreach ($h in $hidden) {
    $args_list += "--hidden-import"
    $args_list += $h
}

# Add data files
$args_list += "--add-data"; $args_list += "config.py;."
if (Test-Path "mq_consumer.py") {
    $args_list += "--add-data"; $args_list += "mq_consumer.py;."
}
if (Test-Path "console.html") {
    $args_list += "--add-data"; $args_list += "console.html;."
}
$args_list += "agent.py"

& pyinstaller @args_list
if (-not (Test-Path "dist\shellagent.exe")) { Die "编译失败，未找到 dist\shellagent.exe" }

$sizeMB = [math]::Round((Get-Item "dist\shellagent.exe").Length / 1MB, 1)
Ok "shellagent.exe 大小：${sizeMB} MB"

# ══════════════════════════════════════════════════════════════
# 4. 编译系统托盘应用 tray_app_windows.py
# ══════════════════════════════════════════════════════════════
Step 4 6 "编译系统托盘应用 tray_app_windows.py -> ShellAgentTray.exe"

$tray_args = @(
    "--noconfirm", "--onefile", "--windowed",
    "--name", "ShellAgentTray",
    "--hidden-import", "pystray._win32",
    "--hidden-import", "PIL._tkinter_finder",
    "--add-data", "config.py;.",
    "tray_app_windows.py"
)

& pyinstaller @tray_args
if (-not (Test-Path "dist\ShellAgentTray.exe")) { Die "编译失败，未找到 dist\ShellAgentTray.exe" }

$traySizeMB = [math]::Round((Get-Item "dist\ShellAgentTray.exe").Length / 1MB, 1)
Ok "ShellAgentTray.exe 大小：${traySizeMB} MB"

# ══════════════════════════════════════════════════════════════
# 5. 生成安装/卸载脚本
# ══════════════════════════════════════════════════════════════
Step 5 6 "生成安装脚本"

# 创建暂存目录
$STAGE = "dist\shellagent-windows"
if (Test-Path $STAGE) { Remove-Item -Recurse -Force $STAGE }
New-Item -ItemType Directory -Path $STAGE | Out-Null

# 复制主程序
Copy-Item "dist\shellagent.exe" "$STAGE\shellagent.exe"
Copy-Item "dist\ShellAgentTray.exe" "$STAGE\ShellAgentTray.exe"
if (Test-Path "console.html") {
    Copy-Item "console.html" "$STAGE\console.html"
}

# ── install.bat：双击安装（复制到 Program Files + 创建快捷方式）───
@'
@echo off
chcp 65001 >nul
title Shell Agent 安装程序

:: 请求管理员权限
net session >nul 2>&1
if %errorLevel% neq 0 (
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

echo.
echo ================================================
echo   Shell Agent 安装程序
echo ================================================
echo.

set "INSTALL_DIR=%ProgramFiles%\ShellAgent"
set "SRC_DIR=%~dp0"

:: 创建安装目录
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

:: 复制文件
echo [1/3] 复制文件...
copy /Y "%SRC_DIR%shellagent.exe" "%INSTALL_DIR%\" >nul
copy /Y "%SRC_DIR%ShellAgentTray.exe" "%INSTALL_DIR%\" >nul
if exist "%SRC_DIR%console.html" copy /Y "%SRC_DIR%console.html" "%INSTALL_DIR%\" >nul

:: 创建开始菜单快捷方式
echo [2/3] 创建快捷方式...
powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%APPDATA%\Microsoft\Windows\Start Menu\Programs\Shell Agent.lnk'); $s.TargetPath = '%INSTALL_DIR%\ShellAgentTray.exe'; $s.WorkingDirectory = '%INSTALL_DIR%'; $s.Save()"

:: 添加到开机启动
echo [3/3] 设置开机自启...
powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Shell Agent.lnk'); $s.TargetPath = '%INSTALL_DIR%\ShellAgentTray.exe'; $s.WorkingDirectory = '%INSTALL_DIR%'; $s.Save()"

echo.
echo ================================================
echo   安装完成！
echo ================================================
echo.
echo   安装目录: %INSTALL_DIR%
echo.
echo   即将启动 Shell Agent...
echo   首次运行会弹出配置对话框，请填写：
echo     - 用户 ID
echo     - AES 加密密钥
echo     - 访问 Token
echo     - 监听端口
echo.

:: 启动托盘应用
start "" "%INSTALL_DIR%\ShellAgentTray.exe"

pause
'@ | Set-Content "$STAGE\install.bat" -Encoding UTF8

# ── uninstall.bat：卸载程序 ───
@'
@echo off
chcp 65001 >nul
title Shell Agent 卸载程序

:: 请求管理员权限
net session >nul 2>&1
if %errorLevel% neq 0 (
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

echo.
echo ================================================
echo   Shell Agent 卸载程序
echo ================================================
echo.

set "INSTALL_DIR=%ProgramFiles%\ShellAgent"
set "CONFIG_DIR=%APPDATA%\ShellAgent"

:: 停止进程
echo [1/4] 停止服务...
taskkill /F /IM shellagent.exe 2>nul
taskkill /F /IM ShellAgentTray.exe 2>nul
timeout /t 2 >nul

:: 删除程序文件
echo [2/4] 删除程序文件...
if exist "%INSTALL_DIR%" rmdir /S /Q "%INSTALL_DIR%"

:: 删除快捷方式
echo [3/4] 删除快捷方式...
del /F /Q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Shell Agent.lnk" 2>nul
del /F /Q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Shell Agent.lnk" 2>nul

:: 询问是否删除配置
echo.
set /p DEL_CONFIG="是否删除配置文件？(y/N): "
if /i "%DEL_CONFIG%"=="y" (
    echo [4/4] 删除配置文件...
    if exist "%CONFIG_DIR%" rmdir /S /Q "%CONFIG_DIR%"
    echo   配置文件已删除
) else (
    echo [4/4] 保留配置文件
    echo   配置目录: %CONFIG_DIR%
)

echo.
echo ================================================
echo   卸载完成！
echo ================================================
echo.
pause
'@ | Set-Content "$STAGE\uninstall.bat" -Encoding UTF8

# ── install-service.ps1：注册为 Windows 服务（可选）───
@'
# Shell Agent — Windows 服务安装脚本（可选）
# 如果需要将 Shell Agent 注册为 Windows 服务（后台自动运行，无需登录），
# 以管理员身份运行此脚本。
#
# 注意：一般情况下使用托盘应用即可，无需注册为服务。

$ErrorActionPreference = "Stop"
$EXE = "$PSScriptRoot\shellagent.exe"
$SVC = "ShellAgent"

if (-not (Test-Path $EXE)) {
    Write-Host "错误：未找到 shellagent.exe" -ForegroundColor Red
    exit 1
}

# 读取配置
$configPath = "$env:APPDATA\ShellAgent\config.json"
$TOKEN = "my-secret-token"
$PORT = "8000"
$USERID = ""
$SECRETKEY = ""

if (Test-Path $configPath) {
    $cfg = Get-Content $configPath | ConvertFrom-Json
    if ($cfg.token) { $TOKEN = $cfg.token }
    if ($cfg.port) { $PORT = $cfg.port }
    if ($cfg.user_id) { $USERID = $cfg.user_id }
    if ($cfg.secret_key) { $SECRETKEY = $cfg.secret_key }
}

Write-Host "配置："
Write-Host "  Token: $($TOKEN.Substring(0, [Math]::Min(4, $TOKEN.Length)))***"
Write-Host "  Port:  $PORT"
Write-Host "  User:  $USERID"

# 停止并删除旧服务
if (Get-Service -Name $SVC -ErrorAction SilentlyContinue) {
    Write-Host "停止旧服务..."
    Stop-Service -Name $SVC -Force -ErrorAction SilentlyContinue
    sc.exe delete $SVC | Out-Null
    Start-Sleep 2
}

# 设置系统环境变量
[System.Environment]::SetEnvironmentVariable("AGENT_TOKEN", $TOKEN, "Machine")
[System.Environment]::SetEnvironmentVariable("AGENT_PORT", $PORT, "Machine")
[System.Environment]::SetEnvironmentVariable("AGENT_HOST", "0.0.0.0", "Machine")
[System.Environment]::SetEnvironmentVariable("MQ_USER_ID", $USERID, "Machine")
[System.Environment]::SetEnvironmentVariable("SECRET_KEY", $SECRETKEY, "Machine")

# 注册服务
New-Service -Name $SVC `
    -BinaryPathName "`"$EXE`"" `
    -DisplayName "Shell Agent" `
    -Description "Shell Agent HTTP 服务 + MQ 消费者" `
    -StartupType Automatic

# 启动服务
Start-Service -Name $SVC
Start-Sleep 2

$svc = Get-Service -Name $SVC
if ($svc.Status -eq "Running") {
    Write-Host "服务已启动：http://localhost:$PORT" -ForegroundColor Green
} else {
    Write-Host "服务状态：$($svc.Status)" -ForegroundColor Yellow
}
'@ | Set-Content "$STAGE\install-service.ps1" -Encoding UTF8

# ── README.txt ───
@"
Shell Agent for Windows v$VERSION
================================

快速安装：
  1. 双击 install.bat
  2. 按提示完成安装
  3. 首次运行弹出配置对话框，填写：
     - 用户 ID（MQ 队列名）
     - AES 加密密钥（与服务端一致）
     - 访问 Token
     - 监听端口（默认 8000）
  4. 系统托盘出现图标，服务自动启动

托盘功能：
  - 绿色图标 = 运行中，红色图标 = 已停止
  - 右键菜单：启动/停止/重启服务
  - 打开控制台：浏览器访问 Web 界面
  - 修改配置：重新配置参数
  - 同步密钥：将密钥同步到服务端

访问控制台：
  浏览器打开 http://localhost:8000/console

配置文件位置：
  %APPDATA%\ShellAgent\config.json

日志文件位置：
  %APPDATA%\ShellAgent\agent.log

卸载：
  双击 uninstall.bat

高级：注册为 Windows 服务（可选）
  以管理员身份运行 PowerShell：
  .\install-service.ps1
"@ | Set-Content "$STAGE\README.txt" -Encoding UTF8

Ok "安装脚本生成完毕"

# ══════════════════════════════════════════════════════════════
# 6. 打包成 ZIP
# ══════════════════════════════════════════════════════════════
Step 6 6 "打包成 $ZIP_OUT"

if (Test-Path $ZIP_OUT) { Remove-Item $ZIP_OUT -Force }
Compress-Archive -Path "$STAGE\*" -DestinationPath $ZIP_OUT

# 清理临时文件
Remove-Item -Recurse -Force $STAGE
foreach ($d in @("build", "dist", "__pycache__")) {
    if (Test-Path $d) { Remove-Item -Recurse -Force $d }
}
Get-ChildItem -Filter "*.spec" | Remove-Item -Force -ErrorAction SilentlyContinue

$zipMB = [math]::Round((Get-Item $ZIP_OUT).Length / 1MB, 1)

Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  打包完成！" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  输出文件: $ZIP_OUT (${zipMB} MB)"
Write-Host ""
Write-Host "  包含内容:"
Write-Host "    - shellagent.exe      后台服务（HTTP + MQ 消费者）"
Write-Host "    - ShellAgentTray.exe  系统托盘应用"
Write-Host "    - console.html        Web 控制台"
Write-Host "    - install.bat         安装脚本"
Write-Host "    - uninstall.bat       卸载脚本"
Write-Host ""
Write-Host "  用户安装流程："  -ForegroundColor Cyan
Write-Host "    1. 解压 ZIP"
Write-Host "    2. 双击 install.bat"
Write-Host "    3. 首次运行弹出配置对话框"
Write-Host "    4. 填写用户 ID、AES 密钥、Token、端口"
Write-Host "    5. 系统托盘出现图标，服务自动启动"
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
