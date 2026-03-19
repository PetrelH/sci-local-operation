"""
macOS 本地 Shell Agent
远程通过 HTTP 接口执行本地 Shell 命令

依赖安装：
    pip3 install fastapi uvicorn

启动：
    python3 agent.py
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
TOKEN = os.getenv("AGENT_TOKEN", "my-secret-token")
HOST  = os.getenv("AGENT_HOST", "0.0.0.0")
PORT  = int(os.getenv("AGENT_PORT", "8000"))

OUTPUT_ENCODING  = "utf-8"
BLOCKED_KEYWORDS = ["rm -rf /", "rm -rf ~", ":(){ :|:& };:", "mkfs", "dd if="]

# ─── 持久化工作目录 ───────────────────────────────────────────
_cwd = os.path.expanduser("~")


def get_cwd() -> str:
    return _cwd


def set_cwd(new_path: str):
    global _cwd
    expanded = os.path.expanduser(new_path)
    if not os.path.isabs(expanded):
        expanded = os.path.normpath(os.path.join(_cwd, expanded))
    if os.path.isdir(expanded):
        _cwd = expanded
        return True, expanded
    return False, expanded


def resolve_cd(command: str) -> str | None:
    stripped = command.strip()
    if stripped in ("cd", "cd ~"):
        return os.path.expanduser("~")
    if stripped.startswith("cd ") and ";" not in stripped and "|" not in stripped and "&&" not in stripped:
        return stripped[3:].strip().strip('"').strip("'")
    return None


# ─── 权限检测 ─────────────────────────────────────────────────
PERMISSION_ERRORS = [
    "Operation not permitted",
    "Permission denied",
    "operation not permitted",
    "permission denied",
]

def is_permission_error(stdout: str, stderr: str) -> bool:
    combined = stdout + stderr
    return any(e in combined for e in PERMISSION_ERRORS)


def sudo_retry(command: str, timeout: int) -> dict:
    """
    用 osascript 弹出系统密码框，以 sudo 权限重新执行命令。
    返回和普通执行相同结构的 dict。
    """
    safe_cmd = command.replace("\\", "\\\\").replace('"', '\\"').replace("'", "'\\''")
    safe_cwd = _cwd.replace("'", "'\\''")

    # osascript 弹出授权对话框并用 sudo 执行
    apple_script = f'''
do shell script "cd '{safe_cwd}' && {safe_cmd}" with administrator privileges
'''
    try:
        result = subprocess.run(
            ["osascript", "-e", apple_script],
            capture_output=True,
            timeout=timeout,
            encoding=OUTPUT_ENCODING,
            errors="replace",
        )
        return {
            "stdout":     result.stdout,
            "stderr":     result.stderr,
            "returncode": result.returncode,
            "sudo":       True,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "sudo 执行超时", "returncode": 1, "sudo": True}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": 1, "sudo": True}


# ─── App ─────────────────────────────────────────────────────
app = FastAPI(title="macOS Shell Agent", version="1.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── 模型 ─────────────────────────────────────────────────────
class CmdRequest(BaseModel):
    command: str
    timeout: int = 30
    stream: bool = False
    sudo_on_permission_error: bool = False  # 默认由前端弹确认框，确认后再传 True 重试


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
    cd_target = resolve_cd(command)
    if cd_target is not None:
        ok, resolved = set_cwd(cd_target)
        if ok:
            return f'echo "cwd: {resolved}"'
        else:
            return f'echo "cd: no such directory: {resolved}" >&2; exit 1'
    safe_cwd = _cwd.replace("'", "'\\''")
    return f"cd '{safe_cwd}' && {command}"


# ─── 路由 ─────────────────────────────────────────────────────

@app.get("/")
async def health():
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
    遇到 Operation not permitted / Permission denied 时：
      - sudo_on_permission_error=true（默认）→ 弹出系统密码框用 sudo 重试
      - sudo_on_permission_error=false → 直接返回错误，前端显示授权引导
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

    # ── 权限错误处理 ─────────────────────────────────────────
    sudo_used = False
    if is_permission_error(proc.stdout, proc.stderr) and body.sudo_on_permission_error:
        log("INFO", f"权限不足，弹出 sudo 授权框重试：{body.command!r}")
        sudo_result = sudo_retry(body.command, body.timeout)
        stdout     = sudo_result["stdout"]
        stderr     = sudo_result["stderr"]
        returncode = sudo_result["returncode"]
        sudo_used  = True
    else:
        stdout     = proc.stdout
        stderr     = proc.stderr
        returncode = proc.returncode

    log("INFO", f"done rc={returncode} duration={duration_ms}ms sudo={sudo_used}")

    return {
        "stdout":      stdout,
        "stderr":      stderr,
        "returncode":  returncode,
        "duration_ms": duration_ms,
        "command":     body.command,
        "cwd":         get_cwd(),
        "sudo_used":   sudo_used,
        # 权限错误且未 sudo 时通知前端展示引导
        "permission_error": is_permission_error(stdout, stderr) and not sudo_used,
    }


@app.post("/exec/stream")
async def exec_stream(body: CmdRequest, request: Request, x_token: str = Header(...)):
    """流式执行，权限错误时在流末尾附加 permission_error 标记"""
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
        output_buf = []
        try:
            async for raw_line in proc.stdout:
                line = raw_line.decode(OUTPUT_ENCODING, errors="replace")
                output_buf.append(line)
                yield f"data: {json.dumps({'line': line})}\n\n"
            await proc.wait()

            combined = "".join(output_buf)
            perm_err = is_permission_error(combined, "")

            # 流式模式下权限错误：用 osascript sudo 重试，把结果追加输出
            if perm_err and body.sudo_on_permission_error:
                yield f"data: {json.dumps({'line': '\n[权限不足，正在弹出授权框...]\n'})}\n\n"
                sudo_result = sudo_retry(body.command, body.timeout)
                if sudo_result["stdout"]:
                    yield f"data: {json.dumps({'line': sudo_result['stdout']})}\n\n"
                if sudo_result["stderr"]:
                    yield f"data: {json.dumps({'line': sudo_result['stderr']})}\n\n"
                yield f"data: {json.dumps({'done': True, 'returncode': sudo_result['returncode'], 'cwd': get_cwd(), 'sudo_used': True})}\n\n"
            else:
                yield f"data: {json.dumps({'done': True, 'returncode': proc.returncode, 'cwd': get_cwd(), 'sudo_used': False, 'permission_error': perm_err})}\n\n"

        except asyncio.CancelledError:
            proc.kill()

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/cwd")
async def get_current_dir(x_token: str = Header(...)):
    check_token(x_token)
    return {"cwd": get_cwd()}


@app.post("/cwd")
async def set_current_dir(request: Request, x_token: str = Header(...)):
    check_token(x_token)
    body = await request.json()
    path = body.get("path", "")
    ok, resolved = set_cwd(path)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Directory not found: {resolved}")
    return {"cwd": resolved}


@app.get("/grant-access")
async def grant_access(x_token: str = Header(...)):
    """
    引导用户开启终端完整磁盘访问权限。
    直接用 osascript 打开系统偏好设置到对应页面。
    """
    check_token(x_token)
    script = '''
tell application "System Preferences"
    activate
    set current pane to pane "com.apple.preference.security"
end tell
delay 0.5
tell application "System Events"
    tell process "System Preferences"
        click button "隐私" of tab group 1 of window 1
    end tell
end tell
'''
    try:
        subprocess.Popen(["osascript", "-e", script])
    except Exception:
        # 回退：直接 open 系统设置 URL
        subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"])

    return {
        "message": "已打开系统设置 → 隐私与安全性，请将你的终端或 ShellAgent 加入「完整磁盘访问权限」",
        "steps": [
            "1. 在打开的窗口中找到「完整磁盘访问权限」",
            "2. 点击左下角锁图标解锁",
            "3. 点击 + 添加 ShellAgent 或你的终端 App",
            "4. 重启 ShellAgent 服务",
        ]
    }


@app.get("/info")
async def system_info(x_token: str = Header(...)):
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
    # 兼容 PyDev 调试器：手动创建 uvicorn server 而不使用 loop_factory
    import uvicorn.config
    config = uvicorn.config.Config(app, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    asyncio.run(server.serve())
