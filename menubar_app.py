"""
Shell Agent — 菜单栏管理 App
提供状态栏图标，用户可以：
  - 一眼看到服务运行状态（图标颜色）
  - 启动 / 停止 / 重启服务
  - 一键在浏览器打开 Web 控制台
  - 查看实时日志

依赖：
    pip install rumps pyobjc-framework-Cocoa

打包：
    pyinstaller --noconfirm --windowed --onefile \\
        --name ShellAgentMenu \\
        --add-binary "$(python -c 'import rumps,os; print(os.path.dirname(rumps.__file__))')/":rumps/ \\
        menubar_app.py
"""

import rumps
import subprocess
import webbrowser
import time
import os
import urllib.request
import urllib.error
import json

from config import (
    AGENT_PORT,
    MENUBAR_LABEL  as LABEL,
    MENUBAR_PLIST  as PLIST,
    MENUBAR_BIN    as BIN,
    MENUBAR_LOG    as LOG,
    MENUBAR_WEB_DIR as WEB_DIR,
    MENUBAR_POLL_INTERVAL,
    PRODUCER_API_URL,
    PRODUCER_API_TOKEN,
)

# 标记文件，用于判断是否首次运行
FIRST_RUN_FLAG = os.path.expanduser("~/.shellagent_configured")


# 从 plist 读取端口（运行时实际值，优先于 config.py 默认值）
def _read_port() -> str:
    try:
        out = subprocess.check_output(
            ["/usr/bin/defaults", "read", PLIST, "EnvironmentVariables"],
            stderr=subprocess.DEVNULL, text=True
        )
        for line in out.splitlines():
            if "AGENT_PORT" in line:
                return line.split("=")[-1].strip().strip('";')
    except Exception:
        pass
    return str(AGENT_PORT)

PORT = _read_port()


def _is_running() -> bool:
    try:
        out = subprocess.check_output(
            ["launchctl", "print", f"system/{LABEL}"],
            stderr=subprocess.DEVNULL, text=True
        )
        return "state = running" in out
    except Exception:
        return False


class ShellAgentApp(rumps.App):
    def __init__(self):
        # 用 Unicode 圆点作图标文字（无需 .icns）
        super().__init__("⬤", quit_button=None)
        self._update_icon()

        self.menu = [
            rumps.MenuItem("Shell Agent", callback=None),  # 标题（不可点）
            None,  # 分隔线
            rumps.MenuItem("● 状态检查中…", callback=None),
            None,
            rumps.MenuItem("▶  启动服务",   callback=self.start_service),
            rumps.MenuItem("■  停止服务",   callback=self.stop_service),
            rumps.MenuItem("↺  重启服务",   callback=self.restart_service),
            None,
            rumps.MenuItem("🌐  打开控制台", callback=self.open_console),
            rumps.MenuItem("📋  查看日志",  callback=self.open_log),
            rumps.MenuItem("⚙️  修改配置",  callback=self.edit_config),
            rumps.MenuItem("🔄  同步密钥",  callback=self.sync_key),
            None,
            rumps.MenuItem("退出",          callback=rumps.quit_application),
        ]
        self.menu["Shell Agent"].set_callback(None)

        # 首次运行检测，弹出配置对话框
        self._check_first_run()

        # 后台轮询状态
        self._timer = rumps.Timer(self._poll_status, MENUBAR_POLL_INTERVAL)
        self._timer.start()
        self._poll_status(None)

    def _check_first_run(self):
        """首次运行时弹出配置对话框"""
        if os.path.exists(FIRST_RUN_FLAG):
            return

        # 弹出配置对话框
        self._show_config_dialog(is_first_run=True)

    def _show_config_dialog(self, is_first_run=False):
        """显示配置对话框"""
        title = "Shell Agent 首次配置" if is_first_run else "修改配置"

        # 用户 ID（MQ 队列名）
        userid_window = rumps.Window(
            message="请输入用户 ID（用于 MQ 队列名，如 user123）：",
            title=title,
            default_text="",
            ok="下一步",
            cancel="跳过" if is_first_run else "取消",
            dimensions=(300, 24)
        )
        userid_resp = userid_window.run()

        if not userid_resp.clicked:
            if is_first_run:
                open(FIRST_RUN_FLAG, 'w').close()
                rumps.notification("Shell Agent", "", "已跳过配置，稍后可通过菜单「修改配置」设置", sound=False)
            return

        user_id = userid_resp.text.strip()
        if not user_id:
            rumps.alert("错误", "用户 ID 不能为空")
            return

        # AES 密钥
        aes_window = rumps.Window(
            message="请输入 AES 加密密钥（至少 8 位，与服务端保持一致）：",
            title=title,
            default_text="",
            ok="下一步",
            cancel="取消",
            dimensions=(300, 24)
        )
        aes_resp = aes_window.run()

        if not aes_resp.clicked:
            return

        aes_key = aes_resp.text.strip()
        if len(aes_key) < 8:
            rumps.alert("错误", "AES 密钥至少需要 8 个字符")
            return

        # Token
        token_window = rumps.Window(
            message="请输入访问 Token（至少 6 位）：",
            title=title,
            default_text="my-secret-token",
            ok="下一步",
            cancel="取消",
            dimensions=(300, 24)
        )
        token_resp = token_window.run()

        if not token_resp.clicked:
            return

        token = token_resp.text.strip()
        if len(token) < 6:
            rumps.alert("错误", "Token 至少需要 6 个字符")
            return

        # 端口
        port_window = rumps.Window(
            message="请输入监听端口（1024-65535）：",
            title=title,
            default_text="8000",
            ok="保存配置",
            cancel="取消",
            dimensions=(300, 24)
        )
        port_resp = port_window.run()

        if not port_resp.clicked:
            return

        port = port_resp.text.strip()
        if not port.isdigit() or not (1024 <= int(port) <= 65535):
            rumps.alert("错误", "端口号必须在 1024-65535 之间")
            return

        # 写入配置到 plist（需要管理员权限）
        self._save_config(token, aes_key, port, user_id)

        # 创建标记文件
        open(FIRST_RUN_FLAG, 'w').close()

    def _sync_key_to_producer(self, user_id, secret_key):
        """同步密钥到 Producer API"""
        url = f"{PRODUCER_API_URL}/key/register"
        data = json.dumps({"user_id": user_id, "secret_key": secret_key}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "x-token": PRODUCER_API_TOKEN,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return True, result
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            return False, f"HTTP {e.code}: {body}"
        except urllib.error.URLError as e:
            return False, f"网络错误: {e.reason}"
        except Exception as e:
            return False, str(e)

    def _save_config(self, token, aes_key, port, user_id):
        """保存配置到 plist 文件"""
        # 使用分号连接命令，确保在一行内执行
        cmd = (
            f'/usr/libexec/PlistBuddy -c \\"Set :EnvironmentVariables:AGENT_TOKEN {token}\\" {PLIST}; '
            f'/usr/libexec/PlistBuddy -c \\"Set :EnvironmentVariables:SECRET_KEY {aes_key}\\" {PLIST}; '
            f'/usr/libexec/PlistBuddy -c \\"Set :EnvironmentVariables:AGENT_PORT {port}\\" {PLIST}; '
            f'/usr/libexec/PlistBuddy -c \\"Set :EnvironmentVariables:MQ_USER_ID {user_id}\\" {PLIST}; '
            f'launchctl bootout system {PLIST} 2>/dev/null; '
            f'launchctl bootstrap system {PLIST}'
        )
        try:
            subprocess.run(
                ["osascript", "-e", f'do shell script "{cmd}" with administrator privileges'],
                check=True
            )
            global PORT
            PORT = port
        except subprocess.CalledProcessError as e:
            rumps.alert("错误", f"保存本地配置失败：{e}")
            return

        # 同步密钥到 Producer API
        ok, result = self._sync_key_to_producer(user_id, aes_key)
        if ok:
            rumps.notification(
                "Shell Agent", "",
                f"配置已保存并同步到服务端（用户: {user_id}，端口: {port}）",
                sound=False
            )
        else:
            rumps.notification(
                "Shell Agent", "",
                f"本地配置已保存，但同步到服务端失败: {result}\n请稍后手动同步或检查网络",
                sound=False
            )

    @rumps.clicked("⚙️  修改配置")
    def edit_config(self, _):
        """手动修改配置"""
        self._show_config_dialog(is_first_run=False)

    def _read_plist_env(self, key: str) -> str:
        """从 plist 读取环境变量"""
        try:
            out = subprocess.check_output(
                ["/usr/bin/defaults", "read", PLIST, "EnvironmentVariables"],
                stderr=subprocess.DEVNULL, text=True
            )
            for line in out.splitlines():
                if key in line:
                    return line.split("=")[-1].strip().strip('";')
        except Exception:
            pass
        return ""

    @rumps.clicked("🔄  同步密钥")
    def sync_key(self, _):
        """手动同步密钥到服务端"""
        user_id = self._read_plist_env("MQ_USER_ID")
        secret_key = self._read_plist_env("SECRET_KEY")

        if not user_id or not secret_key:
            rumps.alert("错误", "请先配置用户 ID 和 AES 密钥")
            return

        ok, result = self._sync_key_to_producer(user_id, secret_key)
        if ok:
            rumps.notification("Shell Agent", "", f"密钥已同步到服务端（用户: {user_id}）", sound=False)
        else:
            rumps.alert("同步失败", f"错误: {result}\n\n服务端地址: {PRODUCER_API_URL}")

    # ── 状态轮询 ──────────────────────────────────────────────
    def _poll_status(self, _):
        running = _is_running()
        self._update_icon(running)

        for key in ["● 状态检查中…", "● 运行中", "○ 已停止"]:
            if key in self.menu:
                item = self.menu[key]
                new_title = f"● 运行中  (:{PORT})" if running else "○ 已停止"
                item.title = new_title
                break

    def _update_icon(self, running: bool = None):
        if running is None:
            running = _is_running()
        self.title = "🟢" if running else "🔴"

    # ── 服务控制 ──────────────────────────────────────────────
    def _run_launchctl(self, *args):
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'do shell script "{" ".join(args)}" with administrator privileges'],
                check=True
            )
        except subprocess.CalledProcessError:
            pass  # 用户取消授权

    @rumps.clicked("▶  启动服务")
    def start_service(self, _):
        if _is_running():
            rumps.notification("Shell Agent", "", "服务已在运行中", sound=False)
            return
        self._run_launchctl(f"launchctl bootstrap system {PLIST}")
        time.sleep(1)
        self._poll_status(None)

    @rumps.clicked("■  停止服务")
    def stop_service(self, _):
        self._run_launchctl(f"launchctl bootout system {PLIST}")
        time.sleep(1)
        self._poll_status(None)

    @rumps.clicked("↺  重启服务")
    def restart_service(self, _):
        self._run_launchctl(
            f"launchctl bootout system {PLIST}; "
            f"launchctl bootstrap system {PLIST}"
        )
        time.sleep(1)
        self._poll_status(None)

    # ── 快捷操作 ──────────────────────────────────────────────
    @rumps.clicked("🌐  打开控制台")
    def open_console(self, _):
        webbrowser.open(f"http://localhost:{PORT}/console")

    @rumps.clicked("📋  查看日志")
    def open_log(self, _):
        subprocess.run(["open", "-a", "Console", LOG])


if __name__ == "__main__":
    ShellAgentApp().run()
