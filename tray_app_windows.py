"""
Shell Agent - Windows 系统托盘应用
提供系统托盘图标，用户可以：
  - 查看服务运行状态（图标颜色）
  - 启动 / 停止 / 重启服务
  - 打开 Web 控制台
  - 修改配置
  - 同步密钥到服务端

依赖：
    pip install pystray pillow

打包：
    pyinstaller --noconfirm --windowed --onefile --name ShellAgentTray tray_app_windows.py
"""

import os
import sys
import json
import subprocess
import webbrowser
import threading
import time
import socket
import urllib.request
import urllib.error
from pathlib import Path

# 添加打包后的路径支持
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).parent

# 配置文件路径
CONFIG_DIR = Path(os.environ.get('APPDATA', '')) / 'ShellAgent'
CONFIG_FILE = CONFIG_DIR / 'config.json'
LOG_FILE = CONFIG_DIR / 'agent.log'
FIRST_RUN_FLAG = CONFIG_DIR / '.configured'

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
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def get_agent_exe():
    """获取 agent.exe 路径"""
    if getattr(sys, 'frozen', False):
        # 打包后，shellagent.exe 在同目录
        exe_path = Path(sys.executable).parent / 'shellagent.exe'
        if exe_path.exists():
            return exe_path
    # 开发环境
    return None


def log(msg):
    """写日志"""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f"[{timestamp}] {msg}\n")
    except Exception:
        pass


class ShellAgentTray:
    def __init__(self):
        self.config = load_config()
        self.agent_process = None
        self.icon = None
        self.running = False

    def _check_first_run(self):
        """首次运行检查"""
        if not FIRST_RUN_FLAG.exists():
            self._show_config_dialog(is_first_run=True)

    def _show_config_dialog(self, is_first_run=False):
        """显示配置对话框"""
        try:
            import tkinter as tk
            from tkinter import ttk, messagebox
        except ImportError:
            log("ERROR: tkinter not available")
            return False

        root = tk.Tk()
        title = "Shell Agent 首次配置" if is_first_run else "Shell Agent 配置"
        root.title(title)
        root.geometry("420x400")
        root.resizable(False, False)

        # 居中显示
        root.update_idletasks()
        x = (root.winfo_screenwidth() - 420) // 2
        y = (root.winfo_screenheight() - 400) // 2
        root.geometry(f"+{x}+{y}")

        # 置顶
        root.attributes('-topmost', True)
        root.focus_force()

        frame = ttk.Frame(root, padding=20)
        frame.pack(fill='both', expand=True)

        # 标题
        ttk.Label(frame, text=title, font=('', 12, 'bold')).pack(anchor='w', pady=(0, 15))

        # 用户 ID
        ttk.Label(frame, text="用户 ID（MQ 队列名）:").pack(anchor='w')
        user_id_var = tk.StringVar(value=self.config.get('user_id', ''))
        ttk.Entry(frame, textvariable=user_id_var, width=50).pack(pady=(0, 10), fill='x')

        # AES 密钥
        ttk.Label(frame, text="AES 加密密钥（至少 8 位，与服务端一致）:").pack(anchor='w')
        secret_key_var = tk.StringVar(value=self.config.get('secret_key', ''))
        ttk.Entry(frame, textvariable=secret_key_var, width=50, show='*').pack(pady=(0, 10), fill='x')

        # Token
        ttk.Label(frame, text="访问 Token（至少 6 位）:").pack(anchor='w')
        token_var = tk.StringVar(value=self.config.get('token', 'my-secret-token'))
        ttk.Entry(frame, textvariable=token_var, width=50).pack(pady=(0, 10), fill='x')

        # 端口
        ttk.Label(frame, text="监听端口（1024-65535）:").pack(anchor='w')
        port_var = tk.StringVar(value=str(self.config.get('port', 8000)))
        ttk.Entry(frame, textvariable=port_var, width=50).pack(pady=(0, 10), fill='x')

        # 服务端地址
        ttk.Label(frame, text="Producer 服务端地址:").pack(anchor='w')
        producer_url_var = tk.StringVar(value=self.config.get('producer_url', 'http://10.17.1.17:9000'))
        ttk.Entry(frame, textvariable=producer_url_var, width=50).pack(pady=(0, 15), fill='x')

        result = {'saved': False}

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
            log(f"配置已保存: user_id={user_id}, port={port}")

            # 创建标记文件
            FIRST_RUN_FLAG.parent.mkdir(parents=True, exist_ok=True)
            FIRST_RUN_FLAG.touch()

            # 同步到服务端
            ok, msg = self._sync_key_to_producer()
            if ok:
                messagebox.showinfo("成功", f"配置已保存并同步到服务端\n用户: {user_id}\n端口: {port}")
                log(f"密钥已同步到服务端: user_id={user_id}")
            else:
                messagebox.showwarning("警告", f"配置已保存，但同步到服务端失败:\n{msg}\n\n请稍后点击「同步密钥」重试")
                log(f"密钥同步失败: {msg}")

            result['saved'] = True
            root.destroy()

        def on_skip():
            if is_first_run:
                # 首次运行跳过，创建标记文件
                FIRST_RUN_FLAG.parent.mkdir(parents=True, exist_ok=True)
                FIRST_RUN_FLAG.touch()
                messagebox.showinfo("提示", "已跳过配置，稍后可通过托盘菜单「修改配置」设置")
            root.destroy()

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="保存配置", command=on_save, width=15).pack(side='left', padx=5)
        skip_text = "跳过" if is_first_run else "取消"
        ttk.Button(btn_frame, text=skip_text, command=on_skip, width=15).pack(side='left', padx=5)

        root.protocol("WM_DELETE_WINDOW", on_skip)
        root.mainloop()

        return result['saved']

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
        # 方法1：检查进程
        if self.agent_process and self.agent_process.poll() is None:
            return True
        # 方法2：检查端口
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('127.0.0.1', self.config['port']))
            sock.close()
            return result == 0
        except Exception:
            return False

    def start_service(self, icon=None, item=None):
        """启动服务"""
        if self.is_running():
            self._notify("Shell Agent", "服务已在运行中")
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
            log(f"启动服务: {agent_exe}")

            try:
                with open(LOG_FILE, 'a') as logf:
                    self.agent_process = subprocess.Popen(
                        [str(agent_exe)],
                        env=env,
                        stdout=logf,
                        stderr=logf,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                time.sleep(2)
                if self.is_running():
                    log("服务启动成功")
                    self._notify("Shell Agent", f"服务已启动 (端口: {self.config['port']})")
                else:
                    log("服务启动失败")
                    self._notify("Shell Agent", "服务启动失败，请查看日志")
            except Exception as e:
                log(f"启动服务出错: {e}")
                self._notify("Shell Agent", f"启动失败: {e}")
        else:
            log("未找到 shellagent.exe")
            self._notify("Shell Agent", "未找到 shellagent.exe")

    def stop_service(self, icon=None, item=None):
        """停止服务"""
        if self.agent_process:
            log("停止服务...")
            self.agent_process.terminate()
            try:
                self.agent_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.agent_process.kill()
            self.agent_process = None
            log("服务已停止")
            self._notify("Shell Agent", "服务已停止")
        else:
            # 尝试通过 taskkill 停止
            try:
                subprocess.run(
                    ['taskkill', '/F', '/IM', 'shellagent.exe'],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                self._notify("Shell Agent", "服务已停止")
            except Exception:
                pass

    def restart_service(self, icon=None, item=None):
        """重启服务"""
        log("重启服务...")
        self.stop_service()
        time.sleep(1)
        self.start_service()

    def open_console(self, icon=None, item=None):
        """打开 Web 控制台"""
        url = f"http://localhost:{self.config['port']}/console"
        log(f"打开控制台: {url}")
        webbrowser.open(url)

    def open_log(self, icon=None, item=None):
        """打开日志文件"""
        if LOG_FILE.exists():
            os.startfile(str(LOG_FILE))
        else:
            self._notify("Shell Agent", "日志文件不存在")

    def edit_config(self, icon=None, item=None):
        """修改配置"""
        # 在新线程中打开配置对话框，避免阻塞托盘
        def show_dialog():
            if self._show_config_dialog(is_first_run=False):
                # 配置已更改，重启服务
                self.restart_service()

        threading.Thread(target=show_dialog, daemon=True).start()

    def sync_key(self, icon=None, item=None):
        """手动同步密钥"""
        if not self.config.get('user_id') or not self.config.get('secret_key'):
            self._notify("Shell Agent", "请先配置用户 ID 和 AES 密钥")
            return

        ok, msg = self._sync_key_to_producer()
        if ok:
            log(f"密钥同步成功: user_id={self.config['user_id']}")
            self._notify("Shell Agent", f"密钥已同步到服务端\n用户: {self.config['user_id']}")
        else:
            log(f"密钥同步失败: {msg}")
            self._notify("Shell Agent", f"同步失败: {msg}")

    def quit_app(self, icon=None, item=None):
        """退出应用"""
        log("退出应用")
        self.stop_service()
        if self.icon:
            self.icon.stop()

    def _notify(self, title, message):
        """显示通知"""
        if self.icon:
            try:
                self.icon.notify(message, title)
            except Exception:
                pass

    def _create_icon_image(self, running=False):
        """创建托盘图标"""
        from PIL import Image, ImageDraw

        size = 64
        image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)

        # 根据状态绘制颜色：绿色=运行中，红色=已停止
        color = (0, 180, 0) if running else (200, 0, 0)
        # 绘制圆形
        draw.ellipse([4, 4, size-4, size-4], fill=color)
        # 绘制边框
        draw.ellipse([4, 4, size-4, size-4], outline=(255, 255, 255), width=2)

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

        # 首次运行检查
        self._check_first_run()

        def create_menu():
            running = self.is_running()
            return pystray.Menu(
                item('Shell Agent', None, enabled=False),
                item(self._get_status_text(), None, enabled=False),
                pystray.Menu.SEPARATOR,
                item('启动服务', self.start_service, enabled=not running),
                item('停止服务', self.stop_service, enabled=running),
                item('重启服务', self.restart_service, enabled=running),
                pystray.Menu.SEPARATOR,
                item('打开控制台', self.open_console),
                item('查看日志', self.open_log),
                pystray.Menu.SEPARATOR,
                item('修改配置', self.edit_config),
                item('同步密钥', self.sync_key),
                pystray.Menu.SEPARATOR,
                item('退出', self.quit_app),
            )

        # 创建图标
        self.icon = pystray.Icon(
            'ShellAgent',
            self._create_icon_image(self.is_running()),
            'Shell Agent',
            menu=create_menu()
        )

        # 定时更新状态
        def update_status():
            while self.icon and self.icon.visible:
                try:
                    running = self.is_running()
                    self.icon.icon = self._create_icon_image(running)
                    self.icon.menu = create_menu()
                except Exception:
                    pass
                time.sleep(5)

        # 自动启动服务
        def auto_start():
            time.sleep(1)
            if not self.is_running():
                self.start_service()

        log("Shell Agent Tray 启动")

        # 启动后台线程
        threading.Thread(target=auto_start, daemon=True).start()
        threading.Thread(target=update_status, daemon=True).start()

        # 运行托盘
        self.icon.run()


if __name__ == '__main__':
    app = ShellAgentTray()
    app.run()
