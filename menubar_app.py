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

from config import (
    AGENT_PORT,
    MENUBAR_LABEL  as LABEL,
    MENUBAR_PLIST  as PLIST,
    MENUBAR_BIN    as BIN,
    MENUBAR_LOG    as LOG,
    MENUBAR_WEB_DIR as WEB_DIR,
    MENUBAR_POLL_INTERVAL,
)


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
            None,
            rumps.MenuItem("退出",          callback=rumps.quit_application),
        ]
        self.menu["Shell Agent"].set_callback(None)

        # 后台轮询状态
        self._timer = rumps.Timer(self._poll_status, MENUBAR_POLL_INTERVAL)
        self._timer.start()
        self._poll_status(None)

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
