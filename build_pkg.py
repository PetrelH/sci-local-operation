#!/usr/bin/env python3
"""
Shell Agent — macOS .pkg 打包脚本
用法：python3 build_pkg.py
依赖：pip install pyinstaller rumps pyobjc-framework-Cocoa
"""

import os
import sys
import shutil
import subprocess
import textwrap
from pathlib import Path

from config import (
    APP_NAME,
    PKG_IDENTIFIER,
    PKG_VERSION    as VERSION,
    MIN_MACOS,
)

PKG_OUT = f"{APP_NAME}-{VERSION}.pkg"
ROOT    = Path(__file__).parent.resolve()

# ── 工具函数 ──────────────────────────────────────────────────
def step(n, total, msg):
    print(f"\n\033[0;32m[{n}/{total}]\033[0m {msg}")

def die(msg):
    print(f"\033[0;31m❌  {msg}\033[0m"); sys.exit(1)

def warn(msg):
    print(f"\033[1;33m⚠   {msg}\033[0m")

def run(*args, **kwargs):
    """运行命令，失败时 die"""
    result = subprocess.run(args, **kwargs)
    if result.returncode != 0:
        die(f"命令失败：{' '.join(str(a) for a in args)}")
    return result

def write(path: Path, content: str, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    path.chmod(mode)

# ══════════════════════════════════════════════════════════════
# 1. 前置检查
# ══════════════════════════════════════════════════════════════
step(1, 7, "前置检查")

if not shutil.which("pyinstaller"):
    die("未找到 pyinstaller，请运行：pip install pyinstaller")

try:
    subprocess.run([sys.executable, "-c", "import rumps"], check=True,
                   capture_output=True)
except subprocess.CalledProcessError:
    die("未找到 rumps，请运行：pip install rumps pyobjc-framework-Cocoa")

for f in ["agent.py", "menubar_app.py", "config.py"]:
    if not (ROOT / f).exists():
        die(f"未找到 {f}，请在项目根目录运行此脚本")

if not (ROOT / "console.html").exists():
    warn("未找到 console.html，跳过 Web 控制台打包")

print("✓ 检查通过")

# ══════════════════════════════════════════════════════════════
# 2. 编译后台服务 agent.py
# ══════════════════════════════════════════════════════════════
step(2, 7, "编译后台服务 agent.py")

for d in ["build", "dist", "__pycache__"]:
    shutil.rmtree(ROOT / d, ignore_errors=True)
for spec in ROOT.glob("*.spec"):
    spec.unlink()

hidden = [
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on",
    # MQ 消费者依赖
    "pika", "pika.adapters", "pika.adapters.blocking_connection",
    "cryptography", "cryptography.hazmat", "cryptography.hazmat.primitives",
    "cryptography.hazmat.primitives.ciphers", "cryptography.hazmat.backends",
    "mq_consumer",
]
cmd = [
    "pyinstaller", "--noconfirm", "--onefile", "--name", "shellagent",
    *[arg for h in hidden for arg in ("--hidden-import", h)],
    # 将 config.py 和 mq_consumer.py 一并打包
    "--add-data", "config.py:.",
    "--add-data", "mq_consumer.py:.",
]
if (ROOT / "console.html").exists():
    cmd += ["--add-data", "console.html:."]
cmd.append("agent.py")
run(*cmd, cwd=ROOT)

agent_bin = ROOT / "dist" / "shellagent"
if not agent_bin.exists():
    die("后台服务编译失败，未找到 dist/shellagent")
print(f"✓ shellagent 大小：{agent_bin.stat().st_size // 1024 // 1024} MB")

# ══════════════════════════════════════════════════════════════
# 3. 编译菜单栏 App menubar_app.py
# ══════════════════════════════════════════════════════════════
step(3, 7, "编译菜单栏 App menubar_app.py")

rumps_dir = subprocess.check_output(
    [sys.executable, "-c", "import rumps, os; print(os.path.dirname(rumps.__file__))"],
    text=True
).strip()

run(
    "pyinstaller", "--noconfirm", "--windowed", "--onefile",
    "--name", "ShellAgentMenu",
    "--osx-bundle-identifier", "com.shellagent.menu",
    "--add-binary", f"{rumps_dir}/:rumps/",
    "--add-data", "config.py:.",
    "menubar_app.py",
    cwd=ROOT,
)

menu_bin = ROOT / "dist" / "ShellAgentMenu"
if not menu_bin.exists():
    die("菜单栏 App 编译失败，未找到 dist/ShellAgentMenu")
print(f"✓ ShellAgentMenu 大小：{menu_bin.stat().st_size // 1024 // 1024} MB")

# ══════════════════════════════════════════════════════════════
# 4. 构建 pkg payload
# ══════════════════════════════════════════════════════════════
step(4, 7, "构建 pkg payload")

PKG_ROOT = ROOT / "pkg_root"
shutil.rmtree(PKG_ROOT, ignore_errors=True)

# 4a. 后台服务二进制
bin_dst = PKG_ROOT / "usr/local/bin/shellagent"
bin_dst.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(agent_bin, bin_dst)
bin_dst.chmod(0o755)

# 4b. 菜单栏 App bundle
app_bundle = PKG_ROOT / "Applications/ShellAgentMenu.app/Contents"
(app_bundle / "MacOS").mkdir(parents=True, exist_ok=True)
(app_bundle / "Resources").mkdir(parents=True, exist_ok=True)
menu_dst = app_bundle / "MacOS/ShellAgentMenu"
shutil.copy2(menu_bin, menu_dst)
menu_dst.chmod(0o755)

write(app_bundle / "Info.plist", f"""\
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
      "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
      <key>CFBundleExecutable</key><string>ShellAgentMenu</string>
      <key>CFBundleIdentifier</key><string>com.shellagent.menu</string>
      <key>CFBundleName</key><string>ShellAgentMenu</string>
      <key>CFBundleDisplayName</key><string>Shell Agent</string>
      <key>CFBundleVersion</key><string>{VERSION}</string>
      <key>CFBundleShortVersionString</key><string>{VERSION}</string>
      <key>CFBundlePackageType</key><string>APPL</string>
      <key>LSUIElement</key><true/>
      <key>NSHighResolutionCapable</key><true/>
      <key>LSMinimumSystemVersion</key><string>{MIN_MACOS}</string>
    </dict>
    </plist>
""")

# 4c. Web 控制台
if (ROOT / "console.html").exists():
    share = PKG_ROOT / "usr/local/share/shellagent"
    share.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "console.html", share / "console.html")

# 4d. launchd daemon plist（系统级，后台服务）
write(PKG_ROOT / "Library/LaunchDaemons/com.shellagent.plist", """\
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
      "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
      <key>Label</key><string>com.shellagent</string>
      <key>ProgramArguments</key>
      <array><string>/usr/local/bin/shellagent</string></array>
      <key>EnvironmentVariables</key>
      <dict>
        <key>AGENT_TOKEN</key><string>__TOKEN__</string>
        <key>AGENT_HOST</key><string>0.0.0.0</string>
        <key>AGENT_PORT</key><string>__PORT__</string>
        <key>SECRET_KEY</key><string>__AESKEY__</string>
        <key>MQ_USER_ID</key><string>__USERID__</string>
      </dict>
      <key>RunAtLoad</key><true/>
      <key>KeepAlive</key><true/>
      <key>StandardOutPath</key><string>/var/log/shellagent.log</string>
      <key>StandardErrorPath</key><string>/var/log/shellagent.err</string>
    </dict>
    </plist>
""")

# 4e. launchd agent plist（用户级，菜单栏 App）
write(PKG_ROOT / "Library/LaunchAgents/com.shellagent.menu.plist", """\
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
      "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
      <key>Label</key><string>com.shellagent.menu</string>
      <key>ProgramArguments</key>
      <array>
        <string>/Applications/ShellAgentMenu.app/Contents/MacOS/ShellAgentMenu</string>
      </array>
      <key>RunAtLoad</key><true/>
      <key>KeepAlive</key><false/>
      <key>StandardOutPath</key><string>/tmp/shellagent-menu.log</string>
      <key>StandardErrorPath</key><string>/tmp/shellagent-menu.err</string>
    </dict>
    </plist>
""")

print("✓ Payload 构建完成")
for p in sorted(PKG_ROOT.rglob("*")):
    print(f"  {str(p).replace(str(PKG_ROOT), '')}")

# ══════════════════════════════════════════════════════════════
# 5. 安装脚本（preinstall / postinstall）
# ══════════════════════════════════════════════════════════════
step(5, 7, "生成安装脚本")

SCRIPTS_DIR = ROOT / "pkg_scripts"
shutil.rmtree(SCRIPTS_DIR, ignore_errors=True)
SCRIPTS_DIR.mkdir()

write(SCRIPTS_DIR / "preinstall", """\
    #!/bin/bash
    launchctl bootout system /Library/LaunchDaemons/com.shellagent.plist 2>/dev/null || true
    CONSOLE_UID=$(stat -f "%u" /dev/console 2>/dev/null || echo "")
    [ -n "$CONSOLE_UID" ] && launchctl bootout gui/"$CONSOLE_UID" \\
      /Library/LaunchAgents/com.shellagent.menu.plist 2>/dev/null || true
    exit 0
""", mode=0o755)

write(SCRIPTS_DIR / "postinstall", """\
    #!/bin/bash
    # postinstall：写入默认配置 → 启动服务
    # 注意：AES 密钥等配置由菜单栏 App 首次启动时弹窗收集
    set -e

    DAEMON_PLIST="/Library/LaunchDaemons/com.shellagent.plist"
    AGENT_PLIST="/Library/LaunchAgents/com.shellagent.menu.plist"
    LOG="/var/log/shellagent-install.log"

    log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }
    log "=== Shell Agent postinstall 开始 ==="

    # 使用默认配置（实际配置由菜单栏 App 首次启动时收集）
    TOKEN="my-secret-token"
    AESKEY=""
    PORT="8000"
    USERID=""

    if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1024 ] || [ "$PORT" -gt 65535 ]; then
      log "端口号无效，回退到 8000"; PORT="8000"
    fi

    # 写入 plist
    sed -i '' "s/__TOKEN__/${TOKEN}/g" "$DAEMON_PLIST"
    sed -i '' "s/__AESKEY__/${AESKEY}/g" "$DAEMON_PLIST"
    sed -i '' "s/__PORT__/${PORT}/g"   "$DAEMON_PLIST"
    sed -i '' "s/__USERID__/${USERID}/g" "$DAEMON_PLIST"
    log "配置写入完成：TOKEN=***  AESKEY=***  PORT=${PORT}  USERID=${USERID}"

    # 修复权限
    chown root:wheel "$DAEMON_PLIST" /usr/local/bin/shellagent
    chmod 644 "$DAEMON_PLIST"; chmod 755 /usr/local/bin/shellagent
    log "权限设置完成"

    # 启动后台 daemon（含重试）
    launch_daemon() {
      launchctl bootout system "$DAEMON_PLIST" 2>/dev/null || true
      sleep 1
      launchctl bootstrap system "$DAEMON_PLIST"
      sleep 2
      launchctl kickstart -k system/com.shellagent 2>/dev/null || true
    }

    log "启动后台服务..."
    launch_daemon

    STARTED=0
    for i in 1 2 3 4 5; do
      sleep 2
      if launchctl print system/com.shellagent 2>/dev/null | grep -q "state = running"; then
        STARTED=1; log "✅ 服务已成功启动（第 ${i} 次检查）"; break
      fi
      log "等待服务启动... (${i}/5)"
    done

    if [ "$STARTED" -eq 0 ]; then
      log "⚠ 首次启动未检测到，正在重试..."
      launch_daemon; sleep 3
      if launchctl print system/com.shellagent 2>/dev/null | grep -q "state = running"; then
        log "✅ 重试后服务启动成功"
      else
        log "⚠ 服务未能自动启动，launchd 已注册，下次开机将自动运行"
        log "   立即启动：sudo launchctl kickstart system/com.shellagent"
      fi
    fi

    # 启动菜单栏 App（当前登录用户）
    CONSOLE_UID=$(stat -f "%u" /dev/console 2>/dev/null || echo "")
    if [ -n "$CONSOLE_UID" ] && [ "$CONSOLE_UID" != "0" ]; then
      log "启动菜单栏 App（uid=${CONSOLE_UID})..."
      chown "${CONSOLE_UID}:staff" "$AGENT_PLIST"; chmod 644 "$AGENT_PLIST"
      launchctl bootout gui/"$CONSOLE_UID" "$AGENT_PLIST" 2>/dev/null || true
      sleep 1
      launchctl bootstrap gui/"$CONSOLE_UID" "$AGENT_PLIST" 2>/dev/null \\
        && launchctl kickstart -k gui/"$CONSOLE_UID"/com.shellagent.menu 2>/dev/null \\
        && log "✅ 菜单栏 App 已启动" \\
        || log "⚠ 菜单栏 App 将在下次登录时自动启动"
    else
      log "未检测到登录用户，菜单栏 App 将在下次登录时自动启动"
    fi

    log "=== postinstall 完成，端口 ${PORT} ==="
""", mode=0o755)

print("✓ 安装脚本生成完毕")

# ══════════════════════════════════════════════════════════════
# 6. 安装向导 HTML 页面
# ══════════════════════════════════════════════════════════════
step(6, 7, "生成安装向导页面")

RES_DIR = ROOT / "pkg_resources"
shutil.rmtree(RES_DIR, ignore_errors=True)
RES_DIR.mkdir()

write(RES_DIR / "welcome.html", """\
    <!DOCTYPE html><html><head><meta charset="utf-8">
    <style>
      body { font-family:-apple-system,sans-serif; padding:20px 24px; color:#1d1d1f; }
      h2   { font-size:17px; font-weight:600; margin:0 0 12px; }
      p    { font-size:13px; line-height:1.6; color:#3d3d3d; margin:0 0 10px; }
      ul   { font-size:13px; line-height:2.2; color:#3d3d3d; padding-left:20px; }
    </style></head>
    <body>
      <h2>欢迎安装 Shell Agent</h2>
      <p>安装完成后你将获得：</p>
      <ul>
        <li>🔧 后台 HTTP 服务，开机自动运行，无需任何操作</li>
        <li>🟢 菜单栏图标 App，随时查看状态、一键启停、打开控制台</li>
        <li>🌐 浏览器 Web 控制台，远程执行 Shell 命令</li>
      </ul>
      <p style="margin-top:12px">点击「继续」设置访问 <strong>Token</strong>、<strong>AES 加密密钥</strong>和<strong>端口号</strong>。</p>
    </body></html>
""")

write(RES_DIR / "config.html", """\
    <!DOCTYPE html><html><head><meta charset="utf-8">
    <style>
      body { font-family:-apple-system,sans-serif; padding:20px 24px; color:#1d1d1f; }
      h2   { font-size:17px; font-weight:600; margin:0 0 14px; }
      p    { font-size:13px; line-height:1.8; color:#3d3d3d; margin:0 0 12px; }
      .highlight { background:#fff3cd; padding:12px 14px; border-radius:8px; border-left:4px solid #ffc107; margin:16px 0; }
      .highlight strong { color:#856404; }
      ul   { font-size:13px; line-height:2; color:#3d3d3d; padding-left:20px; margin:10px 0; }
      code { font-family:monospace; font-size:12px; background:#f2f2f7; padding:2px 6px; border-radius:4px; }
    </style></head>
    <body>
      <h2>安装须知</h2>
      <div class="highlight">
        <strong>点击「同意」后，将弹出对话框让你输入：</strong>
        <ul>
          <li>访问 Token（API 认证密钥）</li>
          <li>AES 加密密钥（消息加密，两端需一致）</li>
          <li>监听端口（默认 8000）</li>
        </ul>
      </div>
      <p>安装完成后，可随时通过以下方式修改配置：</p>
      <p><code>sudo nano /Library/LaunchDaemons/com.shellagent.plist</code></p>
    </body></html>
""")

write(RES_DIR / "conclusion.html", """\
    <!DOCTYPE html><html><head><meta charset="utf-8">
    <style>
      body { font-family:-apple-system,sans-serif; padding:20px 24px; color:#1d1d1f; }
      h2   { font-size:17px; font-weight:600; margin:0 0 14px; }
      .row { display:flex; align-items:flex-start; gap:10px; margin-bottom:12px; }
      .icon{ font-size:20px; flex-shrink:0; margin-top:1px; }
      .desc strong { display:block; font-size:13px; font-weight:600; margin-bottom:2px; }
      .desc span   { font-size:12px; color:#666; }
      hr   { border:none; border-top:1px solid #e5e5ea; margin:14px 0; }
      code { background:#f2f2f7; padding:1px 5px; border-radius:4px; font-size:11px; font-family:monospace; }
    </style></head>
    <body>
      <h2>✅ 安装完成</h2>
      <div class="row">
        <span class="icon">🟢</span>
        <div class="desc">
          <strong>菜单栏图标已出现在右上角</strong>
          <span>点击图标可启停服务、查看状态、一键打开 Web 控制台</span>
        </div>
      </div>
      <div class="row">
        <span class="icon">🌐</span>
        <div class="desc">
          <strong>Web 控制台</strong>
          <span>点击菜单栏「打开控制台」，或浏览器访问 <code>http://localhost:8000</code></span>
        </div>
      </div>
      <div class="row">
        <span class="icon">⚙️</span>
        <div class="desc">
          <strong>开机自动运行</strong>
          <span>服务和菜单栏 App 均已注册为开机自启，无需任何额外操作</span>
        </div>
      </div>
      <hr>
      <p style="font-size:11px;color:#888">
        卸载：<code>sudo bash /usr/local/share/shellagent/uninstall.sh</code>
      </p>
    </body></html>
""")

print("✓ 向导页面生成完毕")

# ══════════════════════════════════════════════════════════════
# 7. pkgbuild + productbuild
# ══════════════════════════════════════════════════════════════
step(7, 7, "打包")

COMPONENT_PKG = ROOT / "component.pkg"
DIST_XML      = ROOT / "distribution.xml"

DIST_XML.write_text(f"""\
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
  <title>Shell Agent {VERSION}</title>
  <welcome    file="welcome.html"    mime-type="text/html"/>
  <license    file="config.html"     mime-type="text/html"/>
  <conclusion file="conclusion.html" mime-type="text/html"/>
  <allowed-os-versions>
    <os-version min="{MIN_MACOS}"/>
  </allowed-os-versions>
  <options require-scripts="true" customize="never" allow-external-scripts="no"/>
  <choices-outline>
    <line choice="default"/>
  </choices-outline>
  <choice id="default" visible="false">
    <pkg-ref id="{PKG_IDENTIFIER}"/>
  </choice>
  <pkg-ref id="{PKG_IDENTIFIER}" version="{VERSION}" onConclusion="none">component.pkg</pkg-ref>
</installer-gui-script>
""", encoding="utf-8")

run(
    "pkgbuild",
    "--root",             str(PKG_ROOT),
    "--scripts",          str(SCRIPTS_DIR),
    "--identifier",       PKG_IDENTIFIER,
    "--version",          VERSION,
    "--install-location", "/",
    str(COMPONENT_PKG),
)

run(
    "productbuild",
    "--distribution", str(DIST_XML),
    "--resources",    str(RES_DIR),
    "--package-path", str(ROOT),
    PKG_OUT,
)

# ── 清理临时目录 ──────────────────────────────────────────────
for d in [PKG_ROOT, SCRIPTS_DIR, RES_DIR,
          ROOT/"build", ROOT/"dist", ROOT/"__pycache__"]:
    shutil.rmtree(d, ignore_errors=True)
for f in [COMPONENT_PKG, DIST_XML, *ROOT.glob("*.spec")]:
    f.unlink(missing_ok=True)

# ── 完成 ──────────────────────────────────────────────────────
pkg_size = (ROOT / PKG_OUT).stat().st_size // 1024 // 1024
print(f"""
═══════════════════════════════════════════════════
\033[0;32m✅  打包完成！\033[0m
═══════════════════════════════════════════════════
  {PKG_OUT}  ({pkg_size} MB)

📦  用户安装流程（5 步）：
    1. 右键 .pkg → 打开（绕过 Gatekeeper）
    2. 欢迎页 → Continue
    3. 配置页 → 填 Token/端口 → 保存配置 → Agree
    4. 输入 Mac 密码 → Install
    5. 完成 → 状态栏出现 🟢，服务已在后台运行
═══════════════════════════════════════════════════
""")
