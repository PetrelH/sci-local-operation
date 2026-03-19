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

# ─── App ─────────────────────────────────────────────────────
app = FastAPI(title="macOS Shell Agent", version="1.0.0")

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


# ─── 路由 ─────────────────────────────────────────────────────

@app.get("/")
async def health():
    """健康检查"""
    return {
        "status": "ok",
        "platform": platform.system(),
        "python": sys.version,
        "time": datetime.datetime.now().isoformat(),
    }


@app.post("/exec")
async def exec_cmd(body: CmdRequest, request: Request, x_token: str = Header(...)):
    """
    执行 Shell 命令（同步返回）

    请求头：  x-token: <your-token>
    请求体：  {"command": "ipconfig", "timeout": 30}
    返回：    {"stdout": "...", "stderr": "...", "returncode": 0, "duration_ms": 123}
    """
    check_token(x_token)
    check_blocked(body.command)

    log("INFO", f"[{request.client.host}] exec: {body.command!r}")
    start = datetime.datetime.now()

    try:
        proc = subprocess.run(
            body.command,
            shell=True,
            executable="/bin/zsh",   # macOS 默认 shell
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
    log("INFO", f"done rc={proc.returncode} duration={duration_ms}ms")

    return {
        "stdout":      proc.stdout,
        "stderr":      proc.stderr,
        "returncode":  proc.returncode,
        "duration_ms": duration_ms,
        "command":     body.command,
    }


@app.post("/exec/stream")
async def exec_stream(body: CmdRequest, request: Request, x_token: str = Header(...)):
    """
    执行 Shell 命令（流式返回，Server-Sent Events）

    每行输出实时推送，适合长时间运行的命令（如 ping、编译等）

    前端接收示例：
        const es = new EventSource(...)  // 或用 fetch + ReadableStream
    """
    check_token(x_token)
    check_blocked(body.command)
    log("INFO", f"[{request.client.host}] stream: {body.command!r}")

    async def generate():
        proc = await asyncio.create_subprocess_shell(
            body.command,
            executable="/bin/zsh",   # macOS 默认 shell
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
            yield f"data: {json.dumps({'done': True, 'returncode': proc.returncode})}\n\n"
        except asyncio.CancelledError:
            proc.kill()

    return StreamingResponse(generate(), media_type="text/event-stream")


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
        "raw_output": proc.stdout[:2000],   # 截断防止过长
    }


# ─── 启动 ─────────────────────────────────────────────────────
if __name__ == "__main__":
    log("INFO", f"Shell Agent starting on {HOST}:{PORT}")
    log("INFO", f"Token: {TOKEN[:4]}{'*' * (len(TOKEN)-4)}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
