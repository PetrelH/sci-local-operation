"""
Shell Agent — 本地 HTTP 服务
提供 Shell 命令执行接口，支持权限授权和持久化工作目录
"""
import asyncio
import datetime
import json
import os
import platform
import subprocess
import sys
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.config import get_settings
from core.exceptions import (
    AppError, AuthError, CommandBlockedError,
    CommandTimeoutError, PermissionError as AppPermissionError,
    DirectoryNotFoundError,
)
from core.logging import setup_logging
from core.models import ErrorResponse, HealthResponse

settings = get_settings()
log = setup_logging(
    name="agent",
    level=settings.agent.log_level,
    log_file="agent.log",
)

# ── 常量 ──────────────────────────────────────────────────────
VERSION  = "1.2.0"
ENCODING = "utf-8"
BLOCKED  = ["rm -rf /", "rm -rf ~", ":(){ :|:& };:", "mkfs", "dd if="]
PERM_ERR = ["Operation not permitted", "Permission denied",
            "operation not permitted", "permission denied"]

# ── 持久化工作目录 ─────────────────────────────────────────────
_cwd = os.path.expanduser("~")


def get_cwd() -> str:
    return _cwd


def set_cwd(path: str) -> str:
    global _cwd
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.normpath(os.path.join(_cwd, expanded))
    if not os.path.isdir(expanded):
        raise DirectoryNotFoundError(expanded)
    _cwd = expanded
    return _cwd


def resolve_cd(command: str) -> Optional[str]:
    s = command.strip()
    if s in ("cd", "cd ~"):
        return os.path.expanduser("~")
    if s.startswith("cd ") and not any(c in s for c in (";", "|", "&&")):
        return s[3:].strip().strip('"').strip("'")
    return None


def build_shell_command(command: str) -> str:
    cd_target = resolve_cd(command)
    if cd_target is not None:
        new_cwd = set_cwd(cd_target)
        return f'echo "cwd: {new_cwd}"'
    safe = _cwd.replace("'", "'\\''")
    return f"cd '{safe}' && {command}"


def is_permission_error(stdout: str, stderr: str) -> bool:
    return any(e in stdout + stderr for e in PERM_ERR)


def sudo_retry(command: str, timeout: int) -> dict:
    safe_cmd = command.replace("\\", "\\\\").replace('"', '\\"')
    safe_cwd = _cwd.replace("'", "'\\''")
    script   = f'do shell script "cd \'{safe_cwd}\' && {safe_cmd}" with administrator privileges'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=timeout,
            encoding=ENCODING, errors="replace",
        )
        return {
            "stdout": result.stdout, "stderr": result.stderr,
            "returncode": result.returncode, "sudo": True,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "sudo 执行超时", "returncode": 1, "sudo": True}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": 1, "sudo": True}


# ── Pydantic 模型 ──────────────────────────────────────────────

class CmdRequest(BaseModel):
    model_config = {
        "json_schema_extra": {
            "example": {
                "command": "ls -la ~/Desktop",
                "timeout": 30,
                "sudo_on_permission_error": False,
            }
        }
    }

    command:                  str  = Field(...,  description="Shell 命令")
    timeout:                  int  = Field(30,   description="超时秒数", ge=1, le=300)
    stream:                   bool = Field(False, description="是否流式返回")
    sudo_on_permission_error: bool = Field(False, description="权限不足时弹出系统密码框重试")


class CwdRequest(BaseModel):
    path: str = Field(..., description="目标目录路径", example="/Users/user/projects")


# ── 标签 & App ─────────────────────────────────────────────────

tags = [
    {"name": "命令执行", "description": "执行 Shell 命令，支持同步和流式两种模式"},
    {"name": "工作目录", "description": "持久化工作目录管理，cd 跨请求保持状态"},
    {"name": "权限授权", "description": "处理 Operation not permitted 权限问题"},
    {"name": "系统",     "description": "健康检查与系统信息"},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"Shell Agent v{VERSION} 启动 {settings.agent.host}:{settings.agent.port}")
    log.info(f"初始工作目录：{get_cwd()}")
    yield
    log.info("Shell Agent 关闭")


app = FastAPI(
    title="Shell Agent",
    version=VERSION,
    description="""
## Shell Agent

macOS 本地 Shell 执行服务。

### 鉴权
请求头携带：`x-token: <AGENT_TOKEN>`

### 特性
- 持久化工作目录（`cd` 跨请求保持）
- 流式输出（SSE）
- 权限不足自动弹出授权框
- 命令黑名单保护
""",
    openapi_tags=tags,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


# ── 中间件：统一异常处理 ───────────────────────────────────────

from fastapi.responses import JSONResponse

@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    log.warning(f"业务异常 {exc.code}：{exc.message}")
    return JSONResponse(status_code=exc.status, content=exc.to_dict())

@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    log.error(f"未捕获异常：{exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"code": "INTERNAL_ERROR", "message": str(exc)})


# ── 鉴权依赖 ──────────────────────────────────────────────────

def verify_token(x_token: str = Header(..., description="访问 Token")):
    if x_token != settings.agent.token:
        raise AuthError()


# ── 路由 ──────────────────────────────────────────────────────

@app.get(
    "/",
    tags=["系统"],
    summary="健康检查",
    response_model=HealthResponse,
)
async def health():
    return {
        "status":  "ok",
        "version": VERSION,
        "time":    datetime.datetime.now().isoformat(),
        "platform": platform.system(),
        "cwd":     get_cwd(),
    }


@app.post(
    "/exec",
    tags=["命令执行"],
    summary="执行命令（同步）",
    responses={
        200: {"content": {"application/json": {"example": {
            "stdout": "total 0\n", "stderr": "", "returncode": 0,
            "duration_ms": 42, "cwd": "/Users/user", "sudo_used": False,
        }}}},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        408: {"model": ErrorResponse},
    },
)
async def exec_cmd(body: CmdRequest, request: Request, x_token: str = Header(...)):
    verify_token(x_token)

    low = body.command.lower()
    for kw in BLOCKED:
        if kw in low:
            raise CommandBlockedError(kw)

    actual = build_shell_command(body.command)
    log.info(f"[{request.client.host}] exec: {body.command!r} cwd={get_cwd()}")
    start = datetime.datetime.now()

    try:
        proc = subprocess.run(
            actual, shell=True, executable="/bin/zsh",
            capture_output=True, timeout=body.timeout,
            encoding=ENCODING, errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise CommandTimeoutError(body.timeout)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    duration_ms = int((datetime.datetime.now() - start).total_seconds() * 1000)
    sudo_used   = False

    if is_permission_error(proc.stdout, proc.stderr) and body.sudo_on_permission_error:
        log.info(f"权限不足，sudo 重试：{body.command!r}")
        r = sudo_retry(body.command, body.timeout)
        stdout, stderr, returncode, sudo_used = r["stdout"], r["stderr"], r["returncode"], True
    else:
        stdout, stderr, returncode = proc.stdout, proc.stderr, proc.returncode

    log.info(f"完成 rc={returncode} {duration_ms}ms sudo={sudo_used}")
    return {
        "stdout": stdout, "stderr": stderr, "returncode": returncode,
        "duration_ms": duration_ms, "command": body.command,
        "cwd": get_cwd(), "sudo_used": sudo_used,
        "permission_error": is_permission_error(stdout, stderr) and not sudo_used,
    }


@app.post(
    "/exec/stream",
    tags=["命令执行"],
    summary="执行命令（流式 SSE）",
    response_description="Server-Sent Events 流",
)
async def exec_stream(body: CmdRequest, request: Request, x_token: str = Header(...)):
    verify_token(x_token)

    low = body.command.lower()
    for kw in BLOCKED:
        if kw in low:
            raise CommandBlockedError(kw)

    actual = build_shell_command(body.command)
    log.info(f"[{request.client.host}] stream: {body.command!r}")

    async def generate():
        proc = await asyncio.create_subprocess_shell(
            actual, executable="/bin/zsh",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        buf = []
        try:
            async for raw in proc.stdout:
                line = raw.decode(ENCODING, errors="replace")
                buf.append(line)
                yield f"data: {json.dumps({'line': line})}\n\n"
            await proc.wait()
            combined  = "".join(buf)
            perm_err  = is_permission_error(combined, "")

            if perm_err and body.sudo_on_permission_error:
                yield f"data: {json.dumps({'line': '\n[权限不足，正在请求授权...]\n'})}\n\n"
                r = sudo_retry(body.command, body.timeout)
                if r["stdout"]:
                    yield f"data: {json.dumps({'line': r['stdout']})}\n\n"
                yield f"data: {json.dumps({'done': True, 'returncode': r['returncode'], 'cwd': get_cwd(), 'sudo_used': True})}\n\n"
            else:
                yield f"data: {json.dumps({'done': True, 'returncode': proc.returncode, 'cwd': get_cwd(), 'sudo_used': False, 'permission_error': perm_err})}\n\n"
        except asyncio.CancelledError:
            proc.kill()

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/cwd", tags=["工作目录"], summary="查询当前工作目录")
async def get_current_dir(x_token: str = Header(...)):
    verify_token(x_token)
    return {"cwd": get_cwd()}


@app.post("/cwd", tags=["工作目录"], summary="设置工作目录")
async def set_current_dir(body: CwdRequest, x_token: str = Header(...)):
    verify_token(x_token)
    new_cwd = set_cwd(body.path)
    return {"cwd": new_cwd}


@app.get("/grant-access", tags=["权限授权"], summary="打开系统设置授权页")
async def grant_access(x_token: str = Header(...)):
    verify_token(x_token)
    try:
        subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"])
    except Exception as e:
        log.warning(f"打开系统设置失败：{e}")
    return {
        "message": "已打开系统设置，请将 ShellAgent 加入「完整磁盘访问权限」",
        "steps": [
            "1. 找到「完整磁盘访问权限」",
            "2. 点击锁图标解锁",
            "3. 点击 + 添加 ShellAgent",
            "4. 重启 ShellAgent 服务",
        ],
    }


@app.get("/info", tags=["系统"], summary="系统信息")
async def system_info(x_token: str = Header(...)):
    verify_token(x_token)
    return {
        "platform":  platform.system(),
        "node":      platform.node(),
        "release":   platform.release(),
        "processor": platform.processor(),
        "python":    sys.version,
        "cwd":       get_cwd(),
    }


# ── 启动 ──────────────────────────────────────────────────────

if __name__ == "__main__":
    cfg = uvicorn.Config(
        app,
        host=settings.agent.host,
        port=settings.agent.port,
        log_level=settings.agent.log_level.lower(),
    )
    server = uvicorn.Server(cfg)
    asyncio.run(server.serve())
