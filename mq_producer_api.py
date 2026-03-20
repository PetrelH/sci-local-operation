"""
Shell Agent — MQ Producer API（数据库定时任务版）
功能：
  1. 每隔固定时间（默认 5 分钟）从 MySQL 读取待执行任务
  2. AES-256-CBC 加密命令后发送到对应用户的 RabbitMQ 队列
  3. HTTP API 供外部系统手动提交命令 / 查看任务状态

依赖：
    pip install fastapi uvicorn pika cryptography pymysql sqlalchemy apscheduler

环境变量：
    MQ_HOST / MQ_PORT / MQ_USER / MQ_PASS / MQ_VHOST
    DB_HOST / DB_PORT / DB_USER / DB_PASS / DB_NAME
    AES_KEY         AES-256 密钥 base64（必填，32字节）
    API_TOKEN       接口鉴权 Token（默认 producer-secret）
    API_HOST / API_PORT
    POLL_INTERVAL   数据库轮询间隔秒数（默认 300，即 5 分钟）

启动：
    # 首次运行先初始化数据库
    python3 mq_producer_api.py --init-db

    # 启动服务
    python3 mq_producer_api.py
"""

import os
import json
import base64
import uuid
import datetime
import logging
import threading
from contextlib import asynccontextmanager
from typing import Optional

import pika
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as aes_padding
from cryptography.hazmat.backends import default_backend
from sqlalchemy import (
    create_engine, Column, String, Integer, Text, DateTime, Enum
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

# ─── 日志 ────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mq_producer")

# ─── 配置 ────────────────────────────────────────────────────
MQ_HOST       = os.getenv("MQ_HOST",       "localhost")
MQ_PORT       = int(os.getenv("MQ_PORT",   "5672"))
MQ_USER       = os.getenv("MQ_USER",       "guest")
MQ_PASS       = os.getenv("MQ_PASS",       "guest")
MQ_VHOST      = os.getenv("MQ_VHOST",      "/")

DB_HOST       = os.getenv("DB_HOST",       "localhost")
DB_PORT       = int(os.getenv("DB_PORT",   "3306"))
DB_USER       = os.getenv("DB_USER",       "root")
DB_PASS       = os.getenv("DB_PASS",       "")
DB_NAME       = os.getenv("DB_NAME",       "shellagent")

AES_KEY_B64   = os.getenv("AES_KEY",       "")
API_TOKEN     = os.getenv("API_TOKEN",     "producer-secret")
API_HOST      = os.getenv("API_HOST",      "0.0.0.0")
API_PORT      = int(os.getenv("API_PORT",  "9000"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))


# ══════════════════════════════════════════════════════════════
# 数据库模型
# ══════════════════════════════════════════════════════════════

Base = declarative_base()


class CommandTask(Base):
    __tablename__ = "command_tasks"

    id           = Column(String(36),  primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id      = Column(String(64),  nullable=False,  index=True)
    command      = Column(Text,        nullable=False)
    timeout      = Column(Integer,     default=30)
    reply_to     = Column(String(128), nullable=True)
    status       = Column(
        Enum("pending", "sent", "failed", name="task_status"),
        default="pending", index=True
    )
    cmd_id       = Column(String(36),  nullable=True)
    retry_count  = Column(Integer,     default=0)
    max_retries  = Column(Integer,     default=3)
    error_msg    = Column(Text,        nullable=True)
    created_at   = Column(DateTime,    default=datetime.datetime.now)
    sent_at      = Column(DateTime,    nullable=True)
    scheduled_at = Column(DateTime,    nullable=True, index=True)


def make_db_url() -> str:
    return f"mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4"


_engine = None
_SessionFactory = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            make_db_url(),
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=3600,
        )
    return _engine


def get_session() -> Session:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine())
    return _SessionFactory()


def init_db():
    engine = create_engine(make_db_url(), pool_pre_ping=True)
    Base.metadata.create_all(engine)
    log.info("数据库表初始化完成")


# ══════════════════════════════════════════════════════════════
# AES-256-CBC
# ══════════════════════════════════════════════════════════════

def _get_aes_key() -> bytes:
    if not AES_KEY_B64:
        raise ValueError("AES_KEY 未设置")
    key = base64.b64decode(AES_KEY_B64)
    if len(key) != 32:
        raise ValueError(f"AES_KEY 须为 32 字节，当前 {len(key)} 字节")
    return key


def aes_encrypt(plaintext: str) -> dict:
    key = _get_aes_key()
    iv  = os.urandom(16)
    padder = aes_padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode()) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    enc = cipher.encryptor()
    ct  = enc.update(padded) + enc.finalize()
    return {
        "iv":   base64.b64encode(iv).decode(),
        "data": base64.b64encode(ct).decode(),
    }


# ══════════════════════════════════════════════════════════════
# RabbitMQ 客户端
# ══════════════════════════════════════════════════════════════

class MQClient:
    def __init__(self):
        self._conn = None
        self._lock = threading.Lock()

    def _new_conn(self):
        creds  = pika.PlainCredentials(MQ_USER, MQ_PASS)
        params = pika.ConnectionParameters(
            host=MQ_HOST, port=MQ_PORT, virtual_host=MQ_VHOST,
            credentials=creds, heartbeat=60, blocked_connection_timeout=30,
        )
        return pika.BlockingConnection(params)

    def channel(self):
        with self._lock:
            if self._conn is None or self._conn.is_closed:
                log.info(f"连接 RabbitMQ {MQ_HOST}:{MQ_PORT}...")
                self._conn = self._new_conn()
            return self._conn.channel()

    def publish(self, queue: str, body: dict, cmd_id: str, reply_to: str):
        for attempt in range(3):
            try:
                ch = self.channel()
                ch.queue_declare(queue=queue,    durable=True)
                ch.queue_declare(queue=reply_to, durable=True)
                ch.basic_publish(
                    exchange="",
                    routing_key=queue,
                    body=json.dumps(body, ensure_ascii=False).encode(),
                    properties=pika.BasicProperties(
                        delivery_mode=2,
                        correlation_id=cmd_id,
                        reply_to=reply_to,
                        content_type="application/json",
                    ),
                )
                return
            except Exception as e:
                log.warning(f"发布失败（第{attempt+1}次）：{e}")
                self._conn = None
                if attempt == 2:
                    raise

    def close(self):
        with self._lock:
            if self._conn and not self._conn.is_closed:
                self._conn.close()


mq = MQClient()


# ══════════════════════════════════════════════════════════════
# 核心：加密并发送单条任务
# ══════════════════════════════════════════════════════════════

def send_encrypted_command(task: CommandTask) -> str:
    cmd_id   = str(uuid.uuid4())
    queue    = f"agent.{task.user_id}"
    reply_to = task.reply_to or f"result.{task.user_id}"
    msg = {
        "cmd_id":   cmd_id,
        "command":  task.command,
        "timeout":  task.timeout,
        "reply_to": reply_to,
    }
    encrypted = aes_encrypt(json.dumps(msg, ensure_ascii=False))
    mq.publish(queue, encrypted, cmd_id, reply_to)
    return cmd_id


# ══════════════════════════════════════════════════════════════
# 定时轮询：从数据库读取 pending 任务并发送
# ══════════════════════════════════════════════════════════════

def poll_and_send():
    log.info("▶  轮询数据库待发任务...")
    session = get_session()
    sent_count = failed_count = 0
    try:
        now = datetime.datetime.now()
        tasks = (
            session.query(CommandTask)
            .filter(
                CommandTask.status == "pending",
                CommandTask.retry_count < CommandTask.max_retries,
                (CommandTask.scheduled_at == None) | (CommandTask.scheduled_at <= now),
            )
            .with_for_update(skip_locked=True)   # 防多实例重复发送
            .order_by(CommandTask.created_at)
            .limit(100)
            .all()
        )

        if not tasks:
            log.info("   无待发任务")
            return

        log.info(f"   发现 {len(tasks)} 条待发任务")
        for task in tasks:
            try:
                cmd_id = send_encrypted_command(task)
                task.status  = "sent"
                task.cmd_id  = cmd_id
                task.sent_at = datetime.datetime.now()
                session.commit()
                log.info(f"   ✓ id={task.id}  user={task.user_id}  cmd={task.command!r}  cmd_id={cmd_id}")
                sent_count += 1
            except Exception as e:
                task.retry_count += 1
                task.error_msg    = str(e)
                if task.retry_count >= task.max_retries:
                    task.status = "failed"
                    log.error(f"   ✗ 达到最大重试次数  id={task.id}  err={e}")
                else:
                    log.warning(f"   ✗ 发送失败（第{task.retry_count}次）id={task.id}  将重试")
                session.commit()
                failed_count += 1

    except Exception as e:
        log.error(f"轮询异常：{e}", exc_info=True)
        session.rollback()
    finally:
        session.close()
        log.info(f"◀  轮询完成  sent={sent_count}  failed={failed_count}")


scheduler = BackgroundScheduler(timezone="Asia/Shanghai")


# ══════════════════════════════════════════════════════════════
# Pydantic 模型
# ══════════════════════════════════════════════════════════════

class SendRequest(BaseModel):
    user_id:      str           = Field(...,  description="目标用户ID，命令发到 agent.{user_id} 队列", example="user123")
    command:      str           = Field(...,  description="待执行的 Shell 命令", example="df -h")
    timeout:      int           = Field(30,   description="命令超时秒数", ge=1, le=300, example=30)
    reply_to:     Optional[str] = Field(None, description="结果回写队列，默认 result.{user_id}", example=None)
    scheduled_at: Optional[str] = Field(None, description="计划执行时间（ISO8601），空=立即执行", example=None)
    max_retries:  int           = Field(3,    description="最大重试次数", ge=0, le=10, example=3)

    class Config:
        json_schema_extra = {
            "example": {
                "user_id":  "user123",
                "command":  "ls -la ~/Desktop",
                "timeout":  30,
                "reply_to": None,
                "scheduled_at": None,
                "max_retries": 3,
            }
        }


class BatchSendRequest(BaseModel):
    commands: list[SendRequest] = Field(
        ...,
        max_length=50,
        description="命令列表，最多 50 条",
    )

    class Config:
        json_schema_extra = {
            "example": {
                "commands": [
                    {"user_id": "user123", "command": "df -h",    "timeout": 30},
                    {"user_id": "user456", "command": "uptime",   "timeout": 10},
                    {"user_id": "user123", "command": "ls ~/Desktop", "scheduled_at": "2024-06-01T09:00:00"},
                ]
            }
        }


# ══════════════════════════════════════════════════════════════
# 鉴权
# ══════════════════════════════════════════════════════════════

def check_token(x_token: str):
    if x_token != API_TOKEN:
        raise HTTPException(401, "Unauthorized")


# ══════════════════════════════════════════════════════════════
# FastAPI App
# ══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        Base.metadata.create_all(get_engine())
        log.info("数据库连接正常")
    except Exception as e:
        log.error(f"数据库连接失败：{e}")

    scheduler.add_job(
        func=poll_and_send,
        trigger=IntervalTrigger(seconds=POLL_INTERVAL),
        id="db_poll",
        replace_existing=True,
        next_run_time=datetime.datetime.now(),  # 启动时立即执行一次
    )
    scheduler.start()

    log.info(f"✅  MQ Producer API 启动  {API_HOST}:{API_PORT}")
    log.info(f"    RabbitMQ: {MQ_HOST}:{MQ_PORT}  |  MySQL: {DB_HOST}/{DB_NAME}")
    log.info(f"    轮询间隔: {POLL_INTERVAL}s  |  AES: {'已配置' if AES_KEY_B64 else '⚠ 未配置'}")
    yield
    scheduler.shutdown(wait=False)
    mq.close()
    log.info("服务关闭")


tags_metadata = [
    {
        "name": "命令发送",
        "description": "手动提交命令，加密后立即或按计划发送到 RabbitMQ。",
    },
    {
        "name": "任务管理",
        "description": "查询、重试数据库中的命令任务，状态流转：`pending → sent / failed`。",
    },
    {
        "name": "轮询控制",
        "description": "手动触发数据库轮询，无需等待下一个定时周期。",
    },
    {
        "name": "系统",
        "description": "健康检查、AES 密钥生成等工具接口。",
    },
]

app = FastAPI(
    title="Shell Agent — MQ Producer API",
    version="1.0.0",
    description="""
## MQ Producer API

从 MySQL 读取待执行任务，AES-256-CBC 加密后定时发送到 RabbitMQ 对应用户队列。

### 工作流程
```
外部系统 → POST /send → 写入 MySQL
                            ↓
                  每 5 分钟定时轮询 pending 任务
                            ↓
                  AES-256-CBC 加密命令
                            ↓
                  发送到 agent.{user_id} 队列
                            ↓
                  Mac 本地 mq_consumer 消费执行
```

### 鉴权
所有接口需在请求头携带：
```
x-token: <API_TOKEN>
```

### 任务状态
| 状态 | 说明 |
|------|------|
| `pending` | 待发送（初始状态） |
| `sent` | 已加密发送到 MQ |
| `failed` | 超过最大重试次数，发送失败 |

### AES 加密格式
消息体为 JSON，字段如下：
```json
{"iv": "base64...", "data": "base64..."}
```
""",
    openapi_tags=tags_metadata,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ══════════════════════════════════════════════════════════════
# 路由
# ══════════════════════════════════════════════════════════════

@app.get(
    "/health",
    tags=["系统"],
    summary="健康检查",
    response_description="MQ、数据库连接状态及下次轮询时间",
)
async def health():
    mq_ok = db_ok = False
    try:
        mq.channel(); mq_ok = True
    except Exception:
        pass
    try:
        get_engine().connect().close(); db_ok = True
    except Exception:
        pass
    job = scheduler.get_job("db_poll")
    return {
        "status":        "ok" if (mq_ok and db_ok) else "degraded",
        "mq":            {"host": f"{MQ_HOST}:{MQ_PORT}", "ok": mq_ok},
        "db":            {"host": f"{DB_HOST}/{DB_NAME}", "ok": db_ok},
        "poll_interval": f"{POLL_INTERVAL}s",
        "next_poll":     job.next_run_time.isoformat() if job and job.next_run_time else None,
        "aes_ready":     bool(AES_KEY_B64),
        "time":          datetime.datetime.now().isoformat(),
    }


@app.post(
    "/send",
    tags=["命令发送"],
    summary="提交单条命令",
    response_description="任务 ID、状态及发送结果",
    responses={
        200: {
            "content": {
                "application/json": {
                    "example": {
                        "id": "uuid-...",
                        "cmd_id": "uuid-...",
                        "user_id": "user123",
                        "command": "ls -la",
                        "status": "sent",
                        "scheduled_at": None,
                        "created_at": "2024-01-01T09:00:00",
                    }
                }
            }
        },
        401: {"description": "Token 错误"},
        500: {"description": "AES_KEY 未配置或 MQ 连接失败"},
    },
)
async def send_command(body: SendRequest, x_token: str = Header(...)):
    check_token(x_token)
    if not AES_KEY_B64:
        raise HTTPException(500, "AES_KEY 未配置")

    session = get_session()
    try:
        scheduled = datetime.datetime.fromisoformat(body.scheduled_at) if body.scheduled_at else None
        task = CommandTask(
            id=str(uuid.uuid4()), user_id=body.user_id, command=body.command,
            timeout=body.timeout, reply_to=body.reply_to, max_retries=body.max_retries,
            scheduled_at=scheduled, status="pending",
        )
        session.add(task)
        session.commit()
        session.refresh(task)

        # 无计划时间则立即发送，不等下次轮询
        if not scheduled:
            try:
                cmd_id = send_encrypted_command(task)
                task.status  = "sent"
                task.cmd_id  = cmd_id
                task.sent_at = datetime.datetime.now()
                session.commit()
            except Exception as e:
                task.retry_count += 1
                task.error_msg = str(e)
                session.commit()

        return {
            "id": task.id, "cmd_id": task.cmd_id, "user_id": task.user_id,
            "command": task.command, "status": task.status,
            "scheduled_at": task.scheduled_at.isoformat() if task.scheduled_at else None,
            "created_at": task.created_at.isoformat(),
        }
    finally:
        session.close()


@app.post(
    "/send/batch",
    tags=["命令发送"],
    summary="批量提交命令",
    description="最多 50 条，写入数据库后立即发送（无 scheduled_at）或等待计划时间。",
    response_description="每条命令的发送状态",
)
async def send_batch(body: BatchSendRequest, x_token: str = Header(...)):
    check_token(x_token)
    if not AES_KEY_B64:
        raise HTTPException(500, "AES_KEY 未配置")

    session = get_session()
    results = []
    try:
        for item in body.commands:
            scheduled = datetime.datetime.fromisoformat(item.scheduled_at) if item.scheduled_at else None
            task = CommandTask(
                id=str(uuid.uuid4()), user_id=item.user_id, command=item.command,
                timeout=item.timeout, reply_to=item.reply_to, max_retries=item.max_retries,
                scheduled_at=scheduled, status="pending",
            )
            session.add(task)
            session.flush()
            if not scheduled:
                try:
                    cmd_id = send_encrypted_command(task)
                    task.status = "sent"; task.cmd_id = cmd_id
                    task.sent_at = datetime.datetime.now()
                    results.append({"id": task.id, "cmd_id": cmd_id, "status": "sent"})
                except Exception as e:
                    task.retry_count += 1; task.error_msg = str(e)
                    results.append({"id": task.id, "status": f"failed: {e}"})
            else:
                results.append({"id": task.id, "status": "scheduled"})
        session.commit()
    except Exception as e:
        session.rollback()
        raise HTTPException(500, f"批量提交失败：{e}")
    finally:
        session.close()
    return {"total": len(results), "results": results}


@app.get(
    "/tasks",
    tags=["任务管理"],
    summary="查询任务列表",
    description="支持按 `status`（pending/sent/failed）和 `user_id` 过滤，支持分页。",
    response_description="任务列表及总数",
)
async def list_tasks(
    status: Optional[str] = None, user_id: Optional[str] = None,
    limit: int = 20, offset: int = 0, x_token: str = Header(...),
):
    check_token(x_token)
    session = get_session()
    try:
        q = session.query(CommandTask)
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
    finally:
        session.close()


@app.get(
    "/tasks/{task_id}",
    tags=["任务管理"],
    summary="查询单条任务",
    response_description="任务详细信息，含重试次数和错误原因",
    responses={404: {"description": "任务不存在"}},
)
async def get_task(task_id: str, x_token: str = Header(...)):
    check_token(x_token)
    session = get_session()
    try:
        task = session.query(CommandTask).filter(CommandTask.id == task_id).first()
        if not task:
            raise HTTPException(404, f"任务不存在：{task_id}")
        return {
            "id": task.id, "user_id": task.user_id, "command": task.command,
            "timeout": task.timeout, "status": task.status, "cmd_id": task.cmd_id,
            "retry_count": task.retry_count, "max_retries": task.max_retries,
            "error_msg": task.error_msg,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "sent_at": task.sent_at.isoformat() if task.sent_at else None,
            "scheduled_at": task.scheduled_at.isoformat() if task.scheduled_at else None,
        }
    finally:
        session.close()


@app.post(
    "/tasks/{task_id}/retry",
    tags=["任务管理"],
    summary="手动重试失败任务",
    description="将 `failed` 或 `pending` 状态的任务重置为 `pending`，并立即尝试发送。",
    response_description="重试后的任务状态和 cmd_id",
    responses={
        200: {"content": {"application/json": {"example": {"id": "uuid-...", "cmd_id": "uuid-...", "status": "sent"}}}},
        400: {"description": "任务状态不允许重试"},
        404: {"description": "任务不存在"},
    },
)
async def retry_task(task_id: str, x_token: str = Header(...)):
    check_token(x_token)
    session = get_session()
    try:
        task = session.query(CommandTask).filter(CommandTask.id == task_id).first()
        if not task:
            raise HTTPException(404, f"任务不存在：{task_id}")
        if task.status not in ("failed", "pending"):
            raise HTTPException(400, f"只能重试 failed/pending 状态的任务，当前：{task.status}")
        task.status = "pending"; task.retry_count = 0; task.error_msg = None
        session.commit()
        cmd_id = send_encrypted_command(task)
        task.status = "sent"; task.cmd_id = cmd_id; task.sent_at = datetime.datetime.now()
        session.commit()
        return {"id": task_id, "cmd_id": cmd_id, "status": "sent"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(500, f"重试失败：{e}")
    finally:
        session.close()


@app.post(
    "/poll/trigger",
    tags=["轮询控制"],
    summary="手动触发数据库轮询",
    description="立即执行一次 `poll_and_send()`，无需等待下一个定时周期（默认 5 分钟）。",
    response_description="触发时间",
    responses={
        200: {"content": {"application/json": {"example": {"triggered": True, "time": "2024-01-01T09:00:00"}}}},
    },
)
async def trigger_poll(x_token: str = Header(...)):
    check_token(x_token)
    import asyncio
    await asyncio.get_event_loop().run_in_executor(None, poll_and_send)
    return {"triggered": True, "time": datetime.datetime.now().isoformat()}


@app.post(
    "/gen-key",
    tags=["系统"],
    summary="生成 AES-256 密钥",
    description="生成一个新的 32 字节随机密钥（base64 编码），用于 `AES_KEY` 环境变量。**生产环境请妥善保存，消费端需使用同一密钥。**",
    response_description="base64 编码的密钥及设置命令",
    responses={
        200: {
            "content": {
                "application/json": {
                    "example": {
                        "key_base64": "abc123...base64...",
                        "usage": 'export AES_KEY="abc123...base64..."',
                    }
                }
            }
        }
    },
)
async def gen_key(x_token: str = Header(...)):
    check_token(x_token)
    key = os.urandom(32)
    b64 = base64.b64encode(key).decode()
    return {"key_base64": b64, "usage": f'export AES_KEY="{b64}"'}


# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if "--init-db" in sys.argv:
        init_db()
        sys.exit(0)
    if not AES_KEY_B64:
        log.warning("⚠  AES_KEY 未设置，可运行：")
        log.warning("   python3 -c \"import os,base64; print(base64.b64encode(os.urandom(32)).decode())\"")
    uvicorn.run(app, host=API_HOST, port=API_PORT, log_level="warning")
