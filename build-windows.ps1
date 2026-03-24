<#
.SYNOPSIS
    Shell Agent - Windows 安装包打包脚本
.DESCRIPTION
    将 Shell Agent 打包为 Windows 安装程序 (.exe)
    依赖：
      - Python 3.8+
      - pip install pyinstaller pystray pillow
      - Inno Setup 6.x (可选，用于生成安装程序)
.NOTES
    用法：powershell -ExecutionPolicy Bypass -File build-windows.ps1
#>

$ErrorActionPreference = "Stop"
$Host.UI.RawUI.WindowTitle = "Shell Agent Windows Build"

# ══════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════
$APP_NAME = "ShellAgent"
$VERSION = "1.0.0"
$PUBLISHER = "Shell Agent"
$ROOT = $PSScriptRoot
$DIST_DIR = Join-Path $ROOT "dist-windows"
$BUILD_DIR = Join-Path $ROOT "build-windows"

# ══════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════
function Write-Step {
    param([int]$n, [int]$total, [string]$msg)
    Write-Host "`n[$n/$total] $msg" -ForegroundColor Green
}

function Write-Success {
    param([string]$msg)
    Write-Host "[OK] $msg" -ForegroundColor Cyan
}

function Write-Warning {
    param([string]$msg)
    Write-Host "[WARN] $msg" -ForegroundColor Yellow
}

function Exit-Error {
    param([string]$msg)
    Write-Host "[ERROR] $msg" -ForegroundColor Red
    exit 1
}

# ══════════════════════════════════════════════════════════════
# 1. 前置检查
# ══════════════════════════════════════════════════════════════
Write-Step 1 6 "前置检查"

# 检查 Python
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Exit-Error "未找到 Python，请先安装 Python 3.8+"
}
$pyVersion = & python --version 2>&1
Write-Host "  Python: $pyVersion"

# 检查 PyInstaller
$pyinstaller = Get-Command pyinstaller -ErrorAction SilentlyContinue
if (-not $pyinstaller) {
    Write-Host "  安装 PyInstaller..." -ForegroundColor Yellow
    & pip install pyinstaller
}

# 检查必要文件
$requiredFiles = @("agent.py", "config.py")
foreach ($f in $requiredFiles) {
    if (-not (Test-Path (Join-Path $ROOT $f))) {
        Exit-Error "未找到 $f，请在项目根目录运行此脚本"
    }
}

# 检查 pystray（系统托盘）
try {
    & python -c "import pystray" 2>$null
} catch {
    Write-Host "  安装 pystray..." -ForegroundColor Yellow
    & pip install pystray pillow
}

Write-Success "检查通过"

# ══════════════════════════════════════════════════════════════
# 2. 创建 Windows 系统托盘应用
# ══════════════════════════════════════════════════════════════
Write-Step 2 6 "生成 Windows 系统托盘应用"

$trayAppPath = Join-Path $ROOT "tray_app_windows.py"
$trayAppContent = @'
"""
Shell Agent - Windows 系统托盘应用
提供系统托盘图标，用户可以：
  - 查看服务运行状态
  - 启动 / 停止服务
  - 打开 Web 控制台
  - 修改配置
"""

import os
import sys
import json
import subprocess
import webbrowser
import threading
import urllib.request
import urllib.error
from pathlib import Path

# 添加打包后的路径支持
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).parent

# 配置文件路径
CONFIG_FILE = Path(os.environ.get('APPDATA', '')) / 'ShellAgent' / 'config.json'
LOG_FILE = Path(os.environ.get('APPDATA', '')) / 'ShellAgent' / 'agent.log'

DEFAULT_CONFIG = {
    'token': 'my-secret-token',
    'port': 8000,
    'host': '0.0.0.0',
    'user_id': '',
    'secret_key': '',
    'producer_url': 'http://10.17.1.17:9000',
    'producer_token': 'producer-secret',
}


def load_config():
    """加载配置"""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
                return {**DEFAULT_CONFIG, **cfg}
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    """保存配置"""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def get_agent_exe():
    """获取 agent.exe 路径"""
    if getattr(sys, 'frozen', False):
        # 打包后，agent.exe 在同目录
        return Path(sys.executable).parent / 'shellagent.exe'
    else:
        return None


class ShellAgentTray:
    def __init__(self):
        self.config = load_config()
        self.agent_process = None
        self.icon = None
        self._first_run_check()

    def _first_run_check(self):
        """首次运行检查"""
        if not CONFIG_FILE.exists():
            self._show_config_dialog()

    def _show_config_dialog(self):
        """显示配置对话框"""
        try:
            import tkinter as tk
            from tkinter import ttk, messagebox
        except ImportError:
            return

        root = tk.Tk()
        root.title("Shell Agent 配置")
        root.geometry("400x350")
        root.resizable(False, False)

        # 居中显示
        root.update_idletasks()
        x = (root.winfo_screenwidth() - 400) // 2
        y = (root.winfo_screenheight() - 350) // 2
        root.geometry(f"+{x}+{y}")

        frame = ttk.Frame(root, padding=20)
        frame.pack(fill='both', expand=True)

        # 用户 ID
        ttk.Label(frame, text="用户 ID（MQ 队列名）:").pack(anchor='w')
        user_id_var = tk.StringVar(value=self.config.get('user_id', ''))
        ttk.Entry(frame, textvariable=user_id_var, width=45).pack(pady=(0, 10))

        # AES 密钥
        ttk.Label(frame, text="AES 加密密钥（至少 8 位）:").pack(anchor='w')
        secret_key_var = tk.StringVar(value=self.config.get('secret_key', ''))
        ttk.Entry(frame, textvariable=secret_key_var, width=45, show='*').pack(pady=(0, 10))

        # Token
        ttk.Label(frame, text="访问 Token:").pack(anchor='w')
        token_var = tk.StringVar(value=self.config.get('token', 'my-secret-token'))
        ttk.Entry(frame, textvariable=token_var, width=45).pack(pady=(0, 10))

        # 端口
        ttk.Label(frame, text="监听端口（1024-65535）:").pack(anchor='w')
        port_var = tk.StringVar(value=str(self.config.get('port', 8000)))
        ttk.Entry(frame, textvariable=port_var, width=45).pack(pady=(0, 10))

        # 服务端地址
        ttk.Label(frame, text="Producer 服务端地址:").pack(anchor='w')
        producer_url_var = tk.StringVar(value=self.config.get('producer_url', 'http://10.17.1.17:9000'))
        ttk.Entry(frame, textvariable=producer_url_var, width=45).pack(pady=(0, 15))

        def on_save():
            user_id = user_id_var.get().strip()
            secret_key = secret_key_var.get().strip()
            token = token_var.get().strip()
            port = port_var.get().strip()
            producer_url = producer_url_var.get().strip()

            # 验证
            if not user_id:
                messagebox.showerror("错误", "用户 ID 不能为空")
                return
            if len(secret_key) < 8:
                messagebox.showerror("错误", "AES 密钥至少需要 8 个字符")
                return
            if len(token) < 6:
                messagebox.showerror("错误", "Token 至少需要 6 个字符")
                return
            if not port.isdigit() or not (1024 <= int(port) <= 65535):
                messagebox.showerror("错误", "端口号必须在 1024-65535 之间")
                return

            # 保存配置
            self.config['user_id'] = user_id
            self.config['secret_key'] = secret_key
            self.config['token'] = token
            self.config['port'] = int(port)
            self.config['producer_url'] = producer_url
            save_config(self.config)

            # 同步到服务端
            ok, msg = self._sync_key_to_producer()
            if ok:
                messagebox.showinfo("成功", f"配置已保存并同步到服务端\n用户: {user_id}\n端口: {port}")
            else:
                messagebox.showwarning("警告", f"配置已保存，但同步到服务端失败:\n{msg}\n\n请稍后手动同步")

            root.destroy()

        ttk.Button(frame, text="保存配置", command=on_save).pack(pady=10)

        root.mainloop()

    def _sync_key_to_producer(self):
        """同步密钥到 Producer API"""
        url = f"{self.config['producer_url']}/key/register"
        data = json.dumps({
            'user_id': self.config['user_id'],
            'secret_key': self.config['secret_key']
        }).encode('utf-8')

        req = urllib.request.Request(
            url,
            data=data,
            headers={
                'Content-Type': 'application/json',
                'x-token': self.config.get('producer_token', 'producer-secret'),
            },
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return True, resp.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}"
        except urllib.error.URLError as e:
            return False, f"网络错误: {e.reason}"
        except Exception as e:
            return False, str(e)

    def is_running(self):
        """检查服务是否运行"""
        if self.agent_process and self.agent_process.poll() is None:
            return True
        # 检查端口是否被占用
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(1)
            result = sock.connect_ex(('127.0.0.1', self.config['port']))
            return result == 0
        except Exception:
            return False
        finally:
            sock.close()

    def start_service(self, icon=None, item=None):
        """启动服务"""
        if self.is_running():
            return

        agent_exe = get_agent_exe()
        if agent_exe and agent_exe.exists():
            # 设置环境变量
            env = os.environ.copy()
            env['AGENT_TOKEN'] = self.config['token']
            env['AGENT_PORT'] = str(self.config['port'])
            env['AGENT_HOST'] = self.config['host']
            env['MQ_USER_ID'] = self.config.get('user_id', '')
            env['SECRET_KEY'] = self.config.get('secret_key', '')

            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_FILE, 'a') as log:
                self.agent_process = subprocess.Popen(
                    [str(agent_exe)],
                    env=env,
                    stdout=log,
                    stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
        self._update_icon()

    def stop_service(self, icon=None, item=None):
        """停止服务"""
        if self.agent_process:
            self.agent_process.terminate()
            try:
                self.agent_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.agent_process.kill()
            self.agent_process = None
        self._update_icon()

    def restart_service(self, icon=None, item=None):
        """重启服务"""
        self.stop_service()
        import time
        time.sleep(1)
        self.start_service()

    def open_console(self, icon=None, item=None):
        """打开 Web 控制台"""
        webbrowser.open(f"http://localhost:{self.config['port']}/console")

    def open_log(self, icon=None, item=None):
        """打开日志文件"""
        if LOG_FILE.exists():
            os.startfile(str(LOG_FILE))

    def edit_config(self, icon=None, item=None):
        """修改配置"""
        self._show_config_dialog()
        self._update_icon()

    def sync_key(self, icon=None, item=None):
        """手动同步密钥"""
        if not self.config.get('user_id') or not self.config.get('secret_key'):
            try:
                import tkinter.messagebox as mb
                mb.showerror("错误", "请先配置用户 ID 和 AES 密钥")
            except Exception:
                pass
            return

        ok, msg = self._sync_key_to_producer()
        try:
            import tkinter.messagebox as mb
            if ok:
                mb.showinfo("成功", f"密钥已同步到服务端\n用户: {self.config['user_id']}")
            else:
                mb.showerror("同步失败", f"错误: {msg}")
        except Exception:
            pass

    def quit_app(self, icon=None, item=None):
        """退出应用"""
        self.stop_service()
        if self.icon:
            self.icon.stop()

    def _update_icon(self):
        """更新图标状态"""
        pass  # pystray 不支持动态更新，需要重建菜单

    def _create_icon(self):
        """创建托盘图标"""
        from PIL import Image, ImageDraw

        # 创建一个简单的圆形图标
        size = 64
        image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)

        # 根据状态绘制颜色
        color = (0, 200, 0) if self.is_running() else (200, 0, 0)
        draw.ellipse([4, 4, size-4, size-4], fill=color)

        return image

    def _get_status_text(self):
        """获取状态文本"""
        if self.is_running():
            return f"● 运行中 (:{self.config['port']})"
        return "○ 已停止"

    def run(self):
        """运行托盘应用"""
        import pystray
        from pystray import MenuItem as item

        def create_menu():
            return pystray.Menu(
                item('Shell Agent', None, enabled=False),
                item(self._get_status_text(), None, enabled=False),
                pystray.Menu.SEPARATOR,
                item('启动服务', self.start_service),
                item('停止服务', self.stop_service),
                item('重启服务', self.restart_service),
                pystray.Menu.SEPARATOR,
                item('打开控制台', self.open_console),
                item('查看日志', self.open_log),
                item('修改配置', self.edit_config),
                item('同步密钥', self.sync_key),
                pystray.Menu.SEPARATOR,
                item('退出', self.quit_app),
            )

        self.icon = pystray.Icon(
            'ShellAgent',
            self._create_icon(),
            'Shell Agent',
            menu=create_menu()
        )

        # 自动启动服务
        threading.Thread(target=self.start_service, daemon=True).start()

        self.icon.run()


if __name__ == '__main__':
    app = ShellAgentTray()
    app.run()
'@

Set-Content -Path $trayAppPath -Value $trayAppContent -Encoding UTF8
Write-Success "系统托盘应用已生成: tray_app_windows.py"

# ══════════════════════════════════════════════════════════════
# 3. 编译后台服务 agent.py
# ══════════════════════════════════════════════════════════════
Write-Step 3 6 "编译后台服务 agent.py"

# 清理旧的构建目录
if (Test-Path $BUILD_DIR) { Remove-Item -Recurse -Force $BUILD_DIR }
if (Test-Path $DIST_DIR) { Remove-Item -Recurse -Force $DIST_DIR }
New-Item -ItemType Directory -Path $DIST_DIR -Force | Out-Null

$hiddenImports = @(
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on",
    "pika", "pika.adapters", "pika.adapters.blocking_connection",
    "cryptography", "cryptography.hazmat", "cryptography.hazmat.primitives",
    "cryptography.hazmat.primitives.ciphers", "cryptography.hazmat.backends",
    "mq_consumer"
)

$pyiArgs = @(
    "pyinstaller",
    "--noconfirm",
    "--onefile",
    "--name", "shellagent",
    "--distpath", $DIST_DIR,
    "--workpath", $BUILD_DIR,
    "--add-data", "config.py;.",
    "--add-data", "mq_consumer.py;."
)

foreach ($h in $hiddenImports) {
    $pyiArgs += "--hidden-import"
    $pyiArgs += $h
}

if (Test-Path (Join-Path $ROOT "console.html")) {
    $pyiArgs += "--add-data"
    $pyiArgs += "console.html;."
}

$pyiArgs += "agent.py"

Push-Location $ROOT
& $pyiArgs[0] $pyiArgs[1..($pyiArgs.Length-1)]
Pop-Location

$agentExe = Join-Path $DIST_DIR "shellagent.exe"
if (-not (Test-Path $agentExe)) {
    Exit-Error "后台服务编译失败，未找到 shellagent.exe"
}
$agentSize = [math]::Round((Get-Item $agentExe).Length / 1MB, 1)
Write-Success "shellagent.exe 大小: $agentSize MB"

# ══════════════════════════════════════════════════════════════
# 4. 编译系统托盘应用
# ══════════════════════════════════════════════════════════════
Write-Step 4 6 "编译系统托盘应用 tray_app_windows.py"

$trayArgs = @(
    "pyinstaller",
    "--noconfirm",
    "--onefile",
    "--windowed",
    "--name", "ShellAgentTray",
    "--distpath", $DIST_DIR,
    "--workpath", $BUILD_DIR,
    "--add-data", "config.py;.",
    "--hidden-import", "pystray._win32",
    "--hidden-import", "PIL._tkinter_finder"
)

$trayArgs += "tray_app_windows.py"

Push-Location $ROOT
& $trayArgs[0] $trayArgs[1..($trayArgs.Length-1)]
Pop-Location

$trayExe = Join-Path $DIST_DIR "ShellAgentTray.exe"
if (-not (Test-Path $trayExe)) {
    Exit-Error "系统托盘应用编译失败，未找到 ShellAgentTray.exe"
}
$traySize = [math]::Round((Get-Item $trayExe).Length / 1MB, 1)
Write-Success "ShellAgentTray.exe 大小: $traySize MB"

# ══════════════════════════════════════════════════════════════
# 5. 生成 Inno Setup 脚本
# ══════════════════════════════════════════════════════════════
Write-Step 5 6 "生成 Inno Setup 安装脚本"

$issPath = Join-Path $ROOT "setup.iss"
$issContent = @"
; Shell Agent Windows Installer Script
; Inno Setup 6.x

#define MyAppName "Shell Agent"
#define MyAppVersion "$VERSION"
#define MyAppPublisher "$PUBLISHER"
#define MyAppURL "https://github.com/shellagent"
#define MyAppExeName "ShellAgentTray.exe"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=$DIST_DIR
OutputBaseFilename=ShellAgent-$VERSION-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
SetupIconFile=
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startupicon"; Description: "开机自动启动"; GroupDescription: "其他选项:"; Flags: checkedonce

[Files]
Source: "$DIST_DIR\shellagent.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "$DIST_DIR\ShellAgentTray.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startupicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "taskkill"; Parameters: "/F /IM shellagent.exe"; Flags: runhidden
Filename: "taskkill"; Parameters: "/F /IM ShellAgentTray.exe"; Flags: runhidden

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssInstall then
  begin
    // 安装前关闭正在运行的程序
    Exec('taskkill', '/F /IM shellagent.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec('taskkill', '/F /IM ShellAgentTray.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;
"@

Set-Content -Path $issPath -Value $issContent -Encoding UTF8
Write-Success "Inno Setup 脚本已生成: setup.iss"

# ══════════════════════════════════════════════════════════════
# 6. 尝试编译安装程序
# ══════════════════════════════════════════════════════════════
Write-Step 6 6 "编译安装程序"

# 查找 Inno Setup 编译器
$isccPaths = @(
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
)

$iscc = $null
foreach ($p in $isccPaths) {
    if (Test-Path $p) {
        $iscc = $p
        break
    }
}

if ($iscc) {
    Write-Host "  使用 Inno Setup: $iscc"
    & $iscc $issPath

    $setupExe = Join-Path $DIST_DIR "ShellAgent-$VERSION-Setup.exe"
    if (Test-Path $setupExe) {
        $setupSize = [math]::Round((Get-Item $setupExe).Length / 1MB, 1)
        Write-Success "安装程序已生成: ShellAgent-$VERSION-Setup.exe ($setupSize MB)"
    }
} else {
    Write-Warning "未找到 Inno Setup，跳过安装程序生成"
    Write-Host "  请安装 Inno Setup 6.x: https://jrsoftware.org/isdl.php"
    Write-Host "  或手动运行: ISCC.exe setup.iss"
}

# ══════════════════════════════════════════════════════════════
# 清理临时文件
# ══════════════════════════════════════════════════════════════
Write-Host "`n清理临时文件..." -ForegroundColor Gray
if (Test-Path $BUILD_DIR) { Remove-Item -Recurse -Force $BUILD_DIR }
Remove-Item -Path (Join-Path $ROOT "*.spec") -Force -ErrorAction SilentlyContinue

# ══════════════════════════════════════════════════════════════
# 完成
# ══════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "═══════════════════════════════════════════════════" -ForegroundColor Green
Write-Host "  打包完成！" -ForegroundColor Green
Write-Host "═══════════════════════════════════════════════════" -ForegroundColor Green
Write-Host ""
Write-Host "  输出目录: $DIST_DIR"
Write-Host "  - shellagent.exe       (后台服务)"
Write-Host "  - ShellAgentTray.exe   (系统托盘应用)"

if (Test-Path (Join-Path $DIST_DIR "ShellAgent-$VERSION-Setup.exe")) {
    Write-Host "  - ShellAgent-$VERSION-Setup.exe (安装程序)"
    Write-Host ""
    Write-Host "  用户安装流程：" -ForegroundColor Cyan
    Write-Host "    1. 双击运行 ShellAgent-$VERSION-Setup.exe"
    Write-Host "    2. 按提示完成安装"
    Write-Host "    3. 系统托盘出现图标，首次运行弹出配置对话框"
    Write-Host "    4. 输入用户 ID、AES 密钥、Token、端口"
    Write-Host "    5. 服务自动启动"
} else {
    Write-Host ""
    Write-Host "  便携版使用方法：" -ForegroundColor Cyan
    Write-Host "    1. 将 dist-windows 文件夹复制到目标机器"
    Write-Host "    2. 运行 ShellAgentTray.exe"
    Write-Host "    3. 首次运行弹出配置对话框"
}

Write-Host ""
Write-Host "═══════════════════════════════════════════════════" -ForegroundColor Green
