"""
macOS 本地 Shell Agent
远程通过 HTTP 接口执行本地 Shell 命令

依赖安装：
    pip3 install fastapi uvicorn

启动：
    python3 agent.py

开机自启（launchd）：
    1. 将下方 plist 保存到 ~/Library/LaunchAgents/com.shellagent.plist
    2. launchctl load ~/Library/LaunchAgents/com.shellagent.plist

plist 内容：
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.shellagent</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/python3</string><string>/path/to/agent.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/shellagent.log</string>
  <key>StandardErrorPath</key><string>/tmp/shellagent.err</string>
</dict></plist>
"""

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import subprocess
import uvicorn
import asyncio
import json
import os
import platform
import datetime
import sys

# ─── 配置 ────────────────────────────────────────────────────
TOKEN = os.getenv("AGENT_TOKEN", "my-secret-token")   # 建议通过环境变量设置
HOST  = os.getenv("AGENT_HOST", "0.0.0.0")
PORT  = int(os.getenv("AGENT_PORT", "8000"))

# macOS / Linux 统一使用 utf-8
OUTPUT_ENCODING = "utf-8"

# 命令黑名单（可按需修改）
BLOCKED_KEYWORDS = ["rm -rf /", "rm -rf ~", ":(){ :|:& };:", "mkfs", "dd if="]

# ─── 持久化工作目录 ───────────────────────────────────────────
# 初始目录设为用户家目录，可访问本机所有有权限的位置
_cwd = os.path.expanduser("~")
_cwd_lock = asyncio.Lock() if False else None   # 同步场景不需要锁，占位


def get_cwd() -> str:
    return _cwd


def set_cwd(new_path: str):
    global _cwd
    # 解析绝对路径（处理 ~ 和相对路径）
    expanded = os.path.expanduser(new_path)
    if not os.path.isabs(expanded):
        expanded = os.path.normpath(os.path.join(_cwd, expanded))
    if os.path.isdir(expanded):
        _cwd = expanded
        return True, expanded
    return False, expanded


def resolve_cd(command: str) -> str | None:
    """
    从命令字符串中提取 cd 目标路径。
    仅处理简单 `cd <path>` 形式（不含管道/分号后的 cd）。
    返回目标路径字符串，或 None（命令不是纯 cd）。
    """
    stripped = command.strip()
    if stripped in ("cd", "cd ~"):
        return os.path.expanduser("~")
    if stripped.startswith("cd ") and ";" not in stripped and "|" not in stripped and "&&" not in stripped:
        return stripped[3:].strip().strip('"').strip("'")
    return None


# ─── App ─────────────────────────────────────────────────────
app = FastAPI(title="macOS Shell Agent", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # 生产环境请改为具体域名
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── 模型 ─────────────────────────────────────────────────────
class CmdRequest(BaseModel):
    command: str
    timeout: int = 30         # 最长执行秒数，默认 30
    stream: bool = False      # 是否流式返回输出


# ─── 工具函数 ─────────────────────────────────────────────────
def check_token(x_token: str):
    if x_token != TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized: invalid token")

def check_blocked(command: str):
    low = command.lower()
    for kw in BLOCKED_KEYWORDS:
        if kw in low:
            raise HTTPException(status_code=403, detail=f"Blocked command keyword: {kw}")

def log(level: str, msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)


def build_command(command: str) -> str:
    """
    在命令前注入 cd 到当前持久目录，确保命令在正确的工作目录下执行。
    同时处理纯 cd 命令：直接更新 _cwd 并返回 echo 确认。
    """
    cd_target = resolve_cd(command)
    if cd_target is not None:
        ok, resolved = set_cwd(cd_target)
        if ok:
            return f'echo "cwd: {resolved}"'
        else:
            return f'echo "cd: no such directory: {resolved}" >&2; exit 1'
    # 普通命令：先 cd 到持久目录再执行
    safe_cwd = _cwd.replace("'", "'\\''")
    return f"cd '{safe_cwd}' && {command}"


# ─── 路由 ─────────────────────────────────────────────────────

@app.get("/")
async def health():
    """健康检查"""
    return {
        "status": "ok",
        "platform": platform.system(),
        "python": sys.version,
        "time": datetime.datetime.now().isoformat(),
        "cwd": get_cwd(),
    }


@app.post("/exec")
async def exec_cmd(body: CmdRequest, request: Request, x_token: str = Header(...)):
    """
    执行 Shell 命令（同步返回）

    请求头：  x-token: <your-token>
    请求体：  {"command": "ls -la", "timeout": 30}
    返回：    {"stdout": "...", "stderr": "...", "returncode": 0, "duration_ms": 123, "cwd": "/current/dir"}
    """
    check_token(x_token)
    check_blocked(body.command)

    actual_command = build_command(body.command)
    log("INFO", f"[{request.client.host}] exec: {body.command!r}  (cwd={get_cwd()})")
    start = datetime.datetime.now()

    try:
        proc = subprocess.run(
            actual_command,
            shell=True,
            executable="/bin/zsh",
            capture_output=True,
            timeout=body.timeout,
            encoding=OUTPUT_ENCODING,
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail=f"Command timed out after {body.timeout}s")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    duration_ms = int((datetime.datetime.now() - start).total_seconds() * 1000)
    log("INFO", f"done rc={proc.returncode} duration={duration_ms}ms cwd={get_cwd()}")

    return {
        "stdout":      proc.stdout,
        "stderr":      proc.stderr,
        "returncode":  proc.returncode,
        "duration_ms": duration_ms,
        "command":     body.command,
        "cwd":         get_cwd(),       # 返回当前目录，方便前端展示
    }


@app.post("/exec/stream")
async def exec_stream(body: CmdRequest, request: Request, x_token: str = Header(...)):
    """
    执行 Shell 命令（流式返回，Server-Sent Events）
    """
    check_token(x_token)
    check_blocked(body.command)

    actual_command = build_command(body.command)
    log("INFO", f"[{request.client.host}] stream: {body.command!r}  (cwd={get_cwd()})")

    async def generate():
        proc = await asyncio.create_subprocess_shell(
            actual_command,
            executable="/bin/zsh",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            async for raw_line in proc.stdout:
                try:
                    line = raw_line.decode(OUTPUT_ENCODING, errors="replace")
                except Exception:
                    line = raw_line.decode("utf-8", errors="replace")
                yield f"data: {json.dumps({'line': line})}\n\n"
            await proc.wait()
            yield f"data: {json.dumps({'done': True, 'returncode': proc.returncode, 'cwd': get_cwd()})}\n\n"
        except asyncio.CancelledError:
            proc.kill()

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/cwd")
async def get_current_dir(x_token: str = Header(...)):
    """返回当前持久工作目录"""
    check_token(x_token)
    return {"cwd": get_cwd()}


@app.post("/cwd")
async def set_current_dir(request: Request, x_token: str = Header(...)):
    """
    手动设置工作目录

    请求体：{"path": "/Users/you/projects"}
    """
    check_token(x_token)
    body = await request.json()
    path = body.get("path", "")
    ok, resolved = set_cwd(path)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Directory not found: {resolved}")
    return {"cwd": resolved}


@app.get("/info")
async def system_info(x_token: str = Header(...)):
    """返回本机基本系统信息"""
    check_token(x_token)
    proc = subprocess.run(
        "system_profiler SPSoftwareDataType SPHardwareDataType",
        shell=True, executable="/bin/zsh",
        capture_output=True, encoding=OUTPUT_ENCODING, errors="replace", timeout=15
    )
    return {
        "platform":   platform.system(),
        "node":       platform.node(),
        "release":    platform.release(),
        "processor":  platform.processor(),
        "cwd":        get_cwd(),
        "raw_output": proc.stdout[:2000],
    }


# ─── 启动 ─────────────────────────────────────────────────────
if __name__ == "__main__":
    log("INFO", f"Shell Agent starting on {HOST}:{PORT}")
    log("INFO", f"Token: {TOKEN[:4]}{'*' * (len(TOKEN)-4)}")
    log("INFO", f"Initial cwd: {get_cwd()}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
