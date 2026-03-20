"""
MQ Producer API
从 MySQL 读取待执行任务，AES-256-CBC 加密后发送到 RabbitMQ
"""
import asyncio
import datetime
import json
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from core.config import get_settings
from core.crypto import encrypt, decrypt, generate_key
from core.exceptions import AppError, AuthError, TaskNotFoundError, TaskStateError
from core.logging import setup_logging
from core.models import ErrorResponse
from infra.database import Database, CommandTask
from infra.mq import MQClient

settings = get_settings()
log      = setup_logging(
    name="producer",
    level=settings.producer.log_level,
    log_file="producer.log",
)

# ── 依赖单例 ──────────────────────────────────────────────────
db  = Database(settings.db)
mq  = MQClient(settings.mq)
scheduler = BackgroundScheduler(timezone=settings.timezone)

VERSION = "1.0.0"


# ── 核心：加密并发送命令 ───────────────────────────────────────

def send_task(task: CommandTask) -> str:
    """加密单条任务并发送到 MQ，返回 cmd_id"""
    cmd_id   = str(uuid.uuid4())
    queue    = f"agent.{task.user_id}"
    reply_to = task.reply_to or f"result.{task.user_id}"
    msg = {
        "cmd_id":   cmd_id,
        "command":  task.command,
        "timeout":  task.timeout,
        "reply_to": reply_to,
    }
    encrypted = encrypt(json.dumps(msg, ensure_ascii=False), settings.crypto.aes_key)
    mq.publish(queue, encrypted, cmd_id, reply_to)
    log.info(f"命令已发送 user={task.user_id} cmd_id={cmd_id} cmd={task.command!r}")
    return cmd_id


# ── 定时任务：轮询数据库 ───────────────────────────────────────

def poll_and_send():
    log.info("▶ 轮询数据库...")
    sent = failed = 0
    try:
        with db.session() as s:
            now   = datetime.datetime.now()
            tasks = (
                s.query(CommandTask)
                .filter(
                    CommandTask.status == "pending",
                    CommandTask.retry_count < CommandTask.max_retries,
                    (CommandTask.scheduled_at == None) | (CommandTask.scheduled_at <= now),
                )
                .with_for_update(skip_locked=True)
                .order_by(CommandTask.created_at)
                .limit(100)
                .all()
            )

            if not tasks:
                log.info("  无待发任务")
                return

            log.info(f"  发现 {len(tasks)} 条任务")
            for task in tasks:
                try:
                    cmd_id = send_task(task)
                    task.status  = "sent"
                    task.cmd_id  = cmd_id
                    task.sent_at = datetime.datetime.now()
                    sent += 1
                except Exception as e:
                    task.retry_count += 1
                    task.error_msg    = str(e)
                    if task.retry_count >= task.max_retries:
                        task.status = "failed"
                        log.error(f"  ✗ 达最大重试 id={task.id} err={e}")
                    else:
                        log.warning(f"  ✗ 发送失败（{task.retry_count}次）id={task.id}")
                    failed += 1
    except Exception as e:
        log.error(f"轮询异常：{e}", exc_info=True)
    finally:
        log.info(f"◀ 轮询完成 sent={sent} failed={failed}")


# ── Pydantic 模型 ──────────────────────────────────────────────

class SendRequest(BaseModel):
    model_config = {
        "json_schema_extra": {
            "example": {
                "user_id": "user123",
                "command": "df -h",
                "timeout": 30,
            }
        }
    }
    user_id:      str           = Field(...,  description="目标用户 ID")
    command:      str           = Field(...,  description="Shell 命令")
    timeout:      int           = Field(30,   ge=1, le=300, description="超时秒数")
    reply_to:     Optional[str] = Field(None, description="结果回写队列，默认 result.{user_id}")
    scheduled_at: Optional[str] = Field(None, description="计划执行时间 ISO8601，空=立即")
    max_retries:  int           = Field(3,    ge=0, le=10, description="最大重试次数")


class BatchSendRequest(BaseModel):
    commands: list[SendRequest] = Field(..., max_length=50, description="命令列表，最多 50 条")


# ── 生命周期 ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    scheduler.add_job(
        poll_and_send,
        trigger=IntervalTrigger(seconds=settings.producer.poll_interval),
        id="db_poll",
        replace_existing=True,
        next_run_time=datetime.datetime.now(),
    )
    scheduler.start()
    log.info(f"MQ Producer API v{VERSION} 启动 {settings.producer.api_host}:{settings.producer.api_port}")
    log.info(f"轮询间隔：{settings.producer.poll_interval}s")
    yield
    scheduler.shutdown(wait=False)
    mq.close()
    db.dispose()
    log.info("服务关闭")


# ── App ───────────────────────────────────────────────────────

tags = [
    {"name": "命令发送", "description": "提交命令，加密后发送到 RabbitMQ"},
    {"name": "任务管理", "description": "查询、重试命令任务"},
    {"name": "轮询控制", "description": "手动触发数据库轮询"},
    {"name": "系统",     "description": "健康检查、密钥生成"},
]

app = FastAPI(
    title="Shell Agent — MQ Producer API",
    version=VERSION,
    description="""
## MQ Producer API

从 MySQL 读取待执行任务，AES-256-CBC 加密后定时发送到 RabbitMQ。

### 鉴权
请求头：`x-token: <PRODUCER_API_TOKEN>`

### 任务状态
`pending` → `sent` | `failed`
""",
    openapi_tags=tags,
    lifespan=lifespan,
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(status_code=exc.status, content=exc.to_dict())


def verify_token(x_token: str = Header(...)):
    if x_token != settings.producer.api_token:
        raise AuthError()


# ── 路由 ──────────────────────────────────────────────────────

@app.get("/health", tags=["系统"], summary="健康检查")
async def health():
    job = scheduler.get_job("db_poll")
    return {
        "status":        "ok",
        "version":       VERSION,
        "mq":            f"{settings.mq.host}:{settings.mq.port}",
        "db":            f"{settings.db.host}/{settings.db.name}",
        "poll_interval": f"{settings.producer.poll_interval}s",
        "next_poll":     job.next_run_time.isoformat() if job and job.next_run_time else None,
        "aes_ready":     bool(settings.crypto.aes_key),
        "time":          datetime.datetime.now().isoformat(),
    }


@app.post("/send", tags=["命令发送"], summary="提交单条命令")
async def send_command(body: SendRequest, x_token: str = Header(...)):
    verify_token(x_token)
    if not settings.crypto.aes_key:
        raise HTTPException(500, "AES_KEY 未配置")

    scheduled = datetime.datetime.fromisoformat(body.scheduled_at) if body.scheduled_at else None

    with db.session() as s:
        task = CommandTask(
            id=str(uuid.uuid4()), user_id=body.user_id, command=body.command,
            timeout=body.timeout, reply_to=body.reply_to, max_retries=body.max_retries,
            scheduled_at=scheduled, status="pending",
        )
        s.add(task)
        s.flush()

        if not scheduled:
            try:
                cmd_id = send_task(task)
                task.status = "sent"; task.cmd_id = cmd_id
                task.sent_at = datetime.datetime.now()
            except Exception as e:
                task.retry_count += 1; task.error_msg = str(e)
                log.error(f"立即发送失败，等待重试：{e}")

    return {
        "id": task.id, "cmd_id": task.cmd_id, "user_id": task.user_id,
        "command": task.command, "status": task.status,
        "scheduled_at": task.scheduled_at.isoformat() if task.scheduled_at else None,
        "created_at": task.created_at.isoformat(),
    }


@app.post("/send/batch", tags=["命令发送"], summary="批量提交命令（最多 50 条）")
async def send_batch(body: BatchSendRequest, x_token: str = Header(...)):
    verify_token(x_token)
    if not settings.crypto.aes_key:
        raise HTTPException(500, "AES_KEY 未配置")

    results = []
    with db.session() as s:
        for item in body.commands:
            scheduled = datetime.datetime.fromisoformat(item.scheduled_at) if item.scheduled_at else None
            task = CommandTask(
                id=str(uuid.uuid4()), user_id=item.user_id, command=item.command,
                timeout=item.timeout, reply_to=item.reply_to, max_retries=item.max_retries,
                scheduled_at=scheduled, status="pending",
            )
            s.add(task); s.flush()
            if not scheduled:
                try:
                    cmd_id = send_task(task)
                    task.status = "sent"; task.cmd_id = cmd_id
                    task.sent_at = datetime.datetime.now()
                    results.append({"id": task.id, "cmd_id": cmd_id, "status": "sent"})
                except Exception as e:
                    task.retry_count += 1; task.error_msg = str(e)
                    results.append({"id": task.id, "status": f"failed: {e}"})
            else:
                results.append({"id": task.id, "status": "scheduled"})

    return {"total": len(results), "results": results}


@app.get("/tasks", tags=["任务管理"], summary="查询任务列表")
async def list_tasks(
    status:  Optional[str] = None,
    user_id: Optional[str] = None,
    limit:   int = 20, offset: int = 0,
    x_token: str = Header(...),
):
    verify_token(x_token)
    with db.session() as s:
        q = s.query(CommandTask)
        if status:  q = q.filter(CommandTask.status == status)
        if user_id: q = q.filter(CommandTask.user_id == user_id)
        total = q.count()
        tasks = q.order_by(CommandTask.created_at.desc()).limit(limit).offset(offset).all()
        return {
            "total": total, "limit": limit, "offset": offset,
            "tasks": [{
                "id": t.id, "user_id": t.user_id, "command": t.command,
                "status": t.status, "cmd_id": t.cmd_id,
                "retry_count": t.retry_count, "error_msg": t.error_msg,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "sent_at": t.sent_at.isoformat() if t.sent_at else None,
                "scheduled_at": t.scheduled_at.isoformat() if t.scheduled_at else None,
            } for t in tasks],
        }


@app.get("/tasks/{task_id}", tags=["任务管理"], summary="查询单条任务")
async def get_task(task_id: str, x_token: str = Header(...)):
    verify_token(x_token)
    with db.session() as s:
        task = s.query(CommandTask).filter(CommandTask.id == task_id).first()
        if not task:
            raise TaskNotFoundError(task_id)
        return {
            "id": task.id, "user_id": task.user_id, "command": task.command,
            "timeout": task.timeout, "status": task.status, "cmd_id": task.cmd_id,
            "retry_count": task.retry_count, "max_retries": task.max_retries,
            "error_msg": task.error_msg,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "sent_at": task.sent_at.isoformat() if task.sent_at else None,
            "scheduled_at": task.scheduled_at.isoformat() if task.scheduled_at else None,
        }


@app.post("/tasks/{task_id}/retry", tags=["任务管理"], summary="手动重试失败任务")
async def retry_task(task_id: str, x_token: str = Header(...)):
    verify_token(x_token)
    with db.session() as s:
        task = s.query(CommandTask).filter(CommandTask.id == task_id).first()
        if not task:
            raise TaskNotFoundError(task_id)
        if task.status not in ("failed", "pending"):
            raise TaskStateError(task.status, ["failed", "pending"])
        task.status = "pending"; task.retry_count = 0; task.error_msg = None
        s.flush()
        cmd_id = send_task(task)
        task.status = "sent"; task.cmd_id = cmd_id; task.sent_at = datetime.datetime.now()
    return {"id": task_id, "cmd_id": cmd_id, "status": "sent"}


@app.post("/poll/trigger", tags=["轮询控制"], summary="手动触发数据库轮询")
async def trigger_poll(x_token: str = Header(...)):
    verify_token(x_token)
    await asyncio.get_event_loop().run_in_executor(None, poll_and_send)
    return {"triggered": True, "time": datetime.datetime.now().isoformat()}


@app.post("/gen-key", tags=["系统"], summary="生成 AES-256 密钥")
async def gen_key(x_token: str = Header(...)):
    verify_token(x_token)
    key = generate_key()
    return {"key_base64": key, "usage": f'AES_KEY="{key}"'}


# ── 启动 ──────────────────────────────────────────────────────

if __name__ == "__main__":
    cfg = uvicorn.Config(
        app,
        host=settings.producer.api_host,
        port=settings.producer.api_port,
        log_level=settings.producer.log_level.lower(),
    )
    server = uvicorn.Server(cfg)
    asyncio.run(server.serve())
