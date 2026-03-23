"""
Shell Agent — MQ Producer API（数据库定时任务版）
功能：
  1. 每隔固定时间（默认 5 分钟）从 MySQL 读取待执行任务
  2. AES-256-CBC 加密命令后发送到对应用户的 RabbitMQ 队列
  3. HTTP API 供外部系统手动提交命令 / 查看任务状态
  4. /key/register 接口：接收 secret_key，派生 AES key = MD5(secret) || MD5(MD5(secret))

依赖：
    pip install fastapi uvicorn pika cryptography pymysql sqlalchemy apscheduler

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
import hashlib
import datetime
import logging
import threading
from contextlib import asynccontextmanager
from typing import Optional

import pika
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict
from typing import Annotated
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as aes_padding
from cryptography.hazmat.backends import default_backend
from sqlalchemy import (
    create_engine, Column, String, Integer, Text, DateTime, Enum
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import (
    MQ_HOST,
    MQ_PORT,
    MQ_USER,
    MQ_PASS,
    MQ_VHOST,
    AES_KEY_B64,
    DB_HOST,
    DB_PORT,
    DB_USER,
    DB_PASS,
    DB_NAME,
    API_TOKEN,
    API_HOST,
    API_PORT,
    POLL_INTERVAL,
)

# ─── 日志 ────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mq_producer")


# ══════════════════════════════════════════════════════════════
# 密钥派生：AES key = MD5(secret) || MD5(MD5(secret)) → 32 bytes
# ══════════════════════════════════════════════════════════════

def derive_aes_key(secret_key: str) -> bytes:
    """
    派生规则：
      first   = MD5(secret_key)   → 16 bytes
      second  = MD5(first)        → 16 bytes
      aes_key = first || second   → 32 bytes（AES-256）
    两端只要 secret_key 相同，派生结果必然一致。
    """
    first  = hashlib.md5(secret_key.encode("utf-8")).digest()
    second = hashlib.md5(first).digest()
    return first + second


def derive_aes_key_b64(secret_key: str) -> str:
    return base64.b64encode(derive_aes_key(secret_key)).decode()


# ══════════════════════════════════════════════════════════════
# 数据库模型
# ══════════════════════════════════════════════════════════════

Base = declarative_base()


class UserKey(Base):
    """用户密钥表：存储原始 secret_key 及派生的 aes_key_b64"""
    __tablename__ = "t_user_keys"

    user_id     = Column(String(64),  primary_key=True, comment="用户ID")
    secret_key  = Column(String(255), nullable=False,   comment="原始 secret key")
    aes_key_b64 = Column(String(255), nullable=False,   comment="派生的 AES-256 key（base64）")
    created_at  = Column(DateTime, default=datetime.datetime.now)
    updated_at  = Column(
        DateTime,
        default=datetime.datetime.now,
        onupdate=datetime.datetime.now,
    )


class CommandTask(Base):
    """命令任务表"""
    __tablename__ = "t_command_tasks"

    id           = Column(String(36),  primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id      = Column(String(64),  nullable=False,  index=True)
    command      = Column(Text,        nullable=False)
    timeout      = Column(Integer,     default=30)
    reply_to     = Column(String(128), nullable=True)
    status       = Column(
        Enum("pending", "sending", "sent", "failed", name="task_status"),
        default="pending", index=True
    )
    cmd_id       = Column(String(36),  nullable=True)
    retry_count  = Column(Integer,     default=0)
    max_retries  = Column(Integer,     default=3)
    error_msg    = Column(Text,        nullable=True)
    created_at   = Column(DateTime,    default=datetime.datetime.now)
    sent_at      = Column(DateTime,    nullable=True)
    scheduled_at = Column(DateTime,    nullable=True, index=True)


class CommandResult(Base):
    """命令执行结果表"""
    __tablename__ = "t_command_results"

    id           = Column(String(36),  primary_key=True, default=lambda: str(uuid.uuid4()))
    cmd_id       = Column(String(36),  nullable=False,  index=True, unique=True, comment="命令ID（correlation_id）")
    user_id      = Column(String(64),  nullable=False,  index=True, comment="用户ID")
    stdout       = Column(Text,        nullable=True,   comment="标准输出")
    stderr       = Column(Text,        nullable=True,   comment="标准错误")
    returncode   = Column(Integer,     nullable=True,   comment="返回码")
    duration_ms  = Column(Integer,     nullable=True,   comment="执行耗时(毫秒)")
    cwd          = Column(String(512), nullable=True,   comment="执行时的工作目录")
    raw_result   = Column(Text,        nullable=True,   comment="原始 JSON 结果")
    received_at  = Column(DateTime,    default=datetime.datetime.now, comment="结果接收时间")


def make_db_url() -> str:
    return (
        f"mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}"
        f"/{DB_NAME}?charset=utf8mb4"
    )


_engine         = None
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
    log.info("数据库表初始化完成（command_tasks + user_keys）")


# ══════════════════════════════════════════════════════════════
# AES-256-CBC 加解密
# ══════════════════════════════════════════════════════════════

def _resolve_raw_key(key_b64: Optional[str]) -> bytes:
    """将 base64 key 解码为 bytes，支持传 None 时 fallback 到全局 AES_KEY_B64"""
    source = key_b64 if key_b64 else AES_KEY_B64
    if not source:
        raise ValueError("未提供 AES key，且未配置全局 AES_KEY 环境变量")
    raw = base64.b64decode(source)
    if len(raw) != 32:
        raise ValueError(f"AES key 须为 32 字节，当前 {len(raw)} 字节")
    return raw


def aes_encrypt(plaintext: str, key_b64: Optional[str] = None) -> dict:
    """
    AES-256-CBC 加密
    key_b64: 用户专属 key（base64），None 时使用全局 AES_KEY 环境变量
    返回: {"iv": base64, "data": base64}
    """
    raw_key = _resolve_raw_key(key_b64)
    iv      = os.urandom(16)
    padder  = aes_padding.PKCS7(128).padder()
    padded  = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    cipher  = Cipher(algorithms.AES(raw_key), modes.CBC(iv), backend=default_backend())
    enc     = cipher.encryptor()
    ct      = enc.update(padded) + enc.finalize()
    return {
        "iv":   base64.b64encode(iv).decode(),
        "data": base64.b64encode(ct).decode(),
    }


def aes_decrypt(payload: dict, key_b64: Optional[str] = None) -> str:
    """
    AES-256-CBC 解密
    payload: {"iv": base64, "data": base64}
    """
    raw_key   = _resolve_raw_key(key_b64)
    iv        = base64.b64decode(payload["iv"])
    ct        = base64.b64decode(payload["data"])
    cipher    = Cipher(algorithms.AES(raw_key), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    padded    = decryptor.update(ct) + decryptor.finalize()
    unpadder  = aes_padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


# ══════════════════════════════════════════════════════════════
# 用户密钥查询
# ══════════════════════════════════════════════════════════════

def get_user_aes_key_b64(user_id: str, session: Session) -> str:
    """
    按 user_id 查询派生后的 aes_key_b64。
    找不到时 fallback 到全局 AES_KEY_B64 环境变量。
    两者都没有则抛 ValueError。
    """
    record = session.query(UserKey).filter(UserKey.user_id == user_id).first()
    if record:
        return record.aes_key_b64
    if AES_KEY_B64:
        log.warning(f"用户 {user_id} 未注册密钥，使用全局 AES_KEY fallback")
        return AES_KEY_B64
    raise ValueError(
        f"用户 {user_id} 未注册密钥，且未配置全局 AES_KEY 环境变量。"
        "请先调用 POST /key/register 注册密钥。"
    )


def save_result_to_db(user_id: str, result: dict, session: Session) -> bool:
    """
    将命令执行结果保存到数据库。
    如果 cmd_id 已存在则跳过（幂等）。
    返回: True=新增, False=已存在
    """
    cmd_id = result.get("cmd_id")
    if not cmd_id:
        log.warning("结果缺少 cmd_id，跳过入库")
        return False

    # 检查是否已存在
    existing = session.query(CommandResult).filter(CommandResult.cmd_id == cmd_id).first()
    if existing:
        log.debug(f"结果已存在，跳过入库：cmd_id={cmd_id}")
        return False

    # 新增记录
    record = CommandResult(
        cmd_id=cmd_id,
        user_id=user_id,
        stdout=result.get("stdout"),
        stderr=result.get("stderr"),
        returncode=result.get("returncode"),
        duration_ms=result.get("duration_ms"),
        cwd=result.get("cwd"),
        raw_result=json.dumps(result, ensure_ascii=False),
    )
    session.add(record)
    session.commit()
    log.info(f"结果已入库：cmd_id={cmd_id}  user_id={user_id}")
    return True


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
                log.warning(f"发布失败（第 {attempt + 1} 次）：{e}")
                self._conn = None
                if attempt == 2:
                    raise

    def get_messages(self, queue: str, max_count: int = 10, auto_ack: bool = True) -> list:
        """
        从队列中获取消息（非阻塞）
        返回: [(body_bytes, properties, delivery_tag), ...]
        """
        messages = []
        try:
            ch = self.channel()
            ch.queue_declare(queue=queue, durable=True)

            for _ in range(max_count):
                method, properties, body = ch.basic_get(queue=queue, auto_ack=auto_ack)
                if method is None:
                    break
                messages.append((body, properties, method.delivery_tag))

            return messages
        except Exception as e:
            log.error(f"获取消息失败：{e}")
            self._conn = None
            raise

    def get_message_by_correlation_id(self, queue: str, correlation_id: str, max_scan: int = 100) -> Optional[tuple]:
        """
        根据 correlation_id 查找特定消息
        返回: (body_bytes, properties) 或 None
        """
        try:
            ch = self.channel()
            ch.queue_declare(queue=queue, durable=True)

            # 临时存储不匹配的消息，扫描后重新入队
            unmatched = []
            result = None

            for _ in range(max_scan):
                method, properties, body = ch.basic_get(queue=queue, auto_ack=False)
                if method is None:
                    break

                if properties.correlation_id == correlation_id:
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                    result = (body, properties)
                    break
                else:
                    # 不匹配的消息暂存，稍后重新入队
                    unmatched.append((body, properties, method.delivery_tag))

            # 将不匹配的消息 nack（重新入队）
            for _, _, delivery_tag in unmatched:
                ch.basic_nack(delivery_tag=delivery_tag, requeue=True)

            return result
        except Exception as e:
            log.error(f"查找消息失败：{e}")
            self._conn = None
            raise

    def close(self):
        with self._lock:
            if self._conn and not self._conn.is_closed:
                self._conn.close()


mq = MQClient()


# ══════════════════════════════════════════════════════════════
# 核心：加密并发送单条任务
# ══════════════════════════════════════════════════════════════

def send_encrypted_command(task: CommandTask, session: Optional[Session] = None) -> str:
    """
    按 task.user_id 查询对应的 AES key，加密命令后发送到 RabbitMQ。
    session 由调用方传入（避免重复开关）；若为 None 则内部自开自关。
    """
    _own_session = session is None
    _session     = get_session() if _own_session else session
    try:
        key_b64  = get_user_aes_key_b64(task.user_id, _session)
        cmd_id   = str(uuid.uuid4())
        queue    = f"agent.{task.user_id}"
        reply_to = task.reply_to or f"result.{task.user_id}"
        msg = {
            "cmd_id":   cmd_id,
            "command":  task.command,
            "timeout":  task.timeout,
            "reply_to": reply_to,
        }
        encrypted = aes_encrypt(json.dumps(msg, ensure_ascii=False), key_b64)
        mq.publish(queue, encrypted, cmd_id, reply_to)
        log.info(
            f"命令已加密发送  user={task.user_id}"
            f"  queue={queue}  cmd_id={cmd_id}"
        )
        return cmd_id
    finally:
        if _own_session:
            _session.close()


# ══════════════════════════════════════════════════════════════
# 定时轮询：从数据库读取 pending 任务并发送
# ══════════════════════════════════════════════════════════════

def poll_and_send():
    log.info("▶  轮询数据库待发任务...")
    session      = get_session()
    sent_count   = 0
    failed_count = 0
    try:
        now   = datetime.datetime.now()
        task_ids = [r[0] for r in (
            session.query(CommandTask.id)
            .filter(
                CommandTask.status == "pending",
                CommandTask.retry_count < CommandTask.max_retries,
                (CommandTask.scheduled_at == None) | (CommandTask.scheduled_at <= now),
            )
            .order_by(CommandTask.created_at)
            .limit(100)
            .all()
        )]

        if not task_ids:
            log.info("   无待发任务")
            return

        # 批量占位，防并发重复
        session.query(CommandTask).filter(
            CommandTask.id.in_(task_ids),
            CommandTask.status == "pending",
        ).update({"status": "sending"}, synchronize_session="fetch")
        session.commit()

        tasks = (
            session.query(CommandTask)
            .filter(CommandTask.id.in_(task_ids), CommandTask.status == "sending")
            .order_by(CommandTask.created_at)
            .all()
        )

        if not tasks:
            log.info("   无待发任务")
            return

        log.info(f"   发现 {len(tasks)} 条待发任务")
        for task in tasks:
            try:
                cmd_id       = send_encrypted_command(task, session)
                task.status  = "sent"
                task.cmd_id  = cmd_id
                task.sent_at = datetime.datetime.now()
                session.commit()
                log.info(
                    f"   ✓ id={task.id}  user={task.user_id}"
                    f"  cmd={task.command!r}  cmd_id={cmd_id}"
                )
                sent_count += 1
            except Exception as e:
                task.retry_count += 1
                task.error_msg    = str(e)
                if task.retry_count >= task.max_retries:
                    task.status = "failed"
                    log.error(f"   ✗ 达到最大重试次数  id={task.id}  err={e}")
                else:
                    task.status = "pending"
                    log.warning(
                        f"   ✗ 发送失败（第 {task.retry_count} 次）"
                        f"  id={task.id}  将重试  err={e}"
                    )
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

class KeyRegisterRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "user_id":    "user123",
                "secret_key": "MyP@ssw0rd!",
            }
        }
    )

    user_id:    str = Field(..., min_length=1, description="用户ID")
    secret_key: str = Field(..., min_length=6, description="App 端输入的原始密钥（至少 6 位）")


class KeyVerifyRequest(BaseModel):
    user_id:    str = Field(..., description="用户ID")
    secret_key: str = Field(..., description="待验证的原始密钥")


class SendRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "user_id":      "user123",
                "command":      "ls -la ~/Desktop",
                "timeout":      30,
                "reply_to":     None,
                "scheduled_at": None,
                "max_retries":  3,
            }
        }
    )

    user_id:      str           = Field(...,  description="目标用户ID，命令发到 agent.{user_id} 队列")
    command:      str           = Field(...,  description="待执行的 Shell 命令")
    timeout:      int           = Field(30,   description="命令超时秒数", ge=1, le=300)
    reply_to:     Optional[str] = Field(None, description="结果回写队列，默认 result.{user_id}")
    scheduled_at: Optional[str] = Field(None, description="计划执行时间（ISO8601），空=立即执行")
    max_retries:  int           = Field(3,    description="最大重试次数", ge=0, le=10)


class BatchSendRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "commands": [
                    {"user_id": "user123", "command": "df -h",       "timeout": 30},
                    {"user_id": "user456", "command": "uptime",       "timeout": 10},
                    {"user_id": "user123", "command": "ls ~/Desktop", "scheduled_at": "2026-04-01T09:00:00"},
                ]
            }
        }
    )

    commands: Annotated[list[SendRequest], Field(max_length=50)] = Field(..., description="命令列表，最多 50 条")


# ══════════════════════════════════════════════════════════════
# 鉴权
# ══════════════════════════════════════════════════════════════

def check_token(x_token: str):
    if x_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized: invalid token")


# ══════════════════════════════════════════════════════════════
# FastAPI App
# ══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        Base.metadata.create_all(get_engine())
        log.info("数据库连接正常，表结构已同步")
    except Exception as e:
        log.error(f"数据库连接失败：{e}")

    scheduler.add_job(
        func=poll_and_send,
        trigger=IntervalTrigger(seconds=POLL_INTERVAL),
        id="db_poll",
        replace_existing=True,
        next_run_time=datetime.datetime.now(),
    )
    scheduler.start()

    log.info(f"✅  MQ Producer API 启动  {API_HOST}:{API_PORT}")
    log.info(f"    RabbitMQ : {MQ_HOST}:{MQ_PORT}  vhost={MQ_VHOST}")
    log.info(f"    MySQL    : {DB_HOST}:{DB_PORT}/{DB_NAME}")
    log.info(f"    轮询间隔 : {POLL_INTERVAL}s")
    log.info(f"    全局 AES : {'已配置（fallback）' if AES_KEY_B64 else '未配置（仅使用用户专属 key）'}")

    yield

    scheduler.shutdown(wait=False)
    mq.close()
    log.info("服务已关闭")


tags_metadata = [
    {"name": "密钥管理", "description": "注册/更新/验证/删除用户密钥，派生规则：MD5(secret) ‖ MD5(MD5(secret))。"},
    {"name": "命令发送", "description": "手动提交命令，加密后立即或按计划发送到 RabbitMQ。"},
    {"name": "结果获取", "description": "从结果队列获取命令执行结果并解密返回。"},
    {"name": "任务管理", "description": "查询、重试数据库中的命令任务，状态流转：pending → sent / failed。"},
    {"name": "轮询控制", "description": "手动触发数据库轮询，无需等待下一个定时周期。"},
    {"name": "系统",     "description": "健康检查、AES 密钥生成等工具接口。"},
]

app = FastAPI(
    title="Shell Agent — MQ Producer API",
    version="2.0.0",
    description="""
## MQ Producer API

从 MySQL 读取待执行任务，AES-256-CBC 加密后定时发送到 RabbitMQ 对应用户队列。

### 密钥派生规则
```
aes_key = MD5(secret_key) || MD5(MD5(secret_key))   →  32 bytes (AES-256)
```
App 端调用 `POST /key/register` 传入 `secret_key`，服务端派生并存储 AES key。
Consumer 端用**相同的 secret_key** 本地派生，AES key 永不通过网络传输。

### 工作流程
```
App → POST /key/register {user_id, secret_key}
               ↓ 派生 aes_key 存入 user_keys 表
App → POST /send {user_id, command}
               ↓ 用该用户的 aes_key 加密
          RabbitMQ agent.{user_id}
               ↓
          Consumer 本地派生 aes_key 解密执行
               ↓
          RabbitMQ result.{user_id}（加密结果）
```

### 任务状态
| 状态 | 说明 |
|------|------|
| `pending` | 待发送（初始状态） |
| `sent`    | 已加密发送到 MQ   |
| `failed`  | 超过最大重试次数   |
""",
    openapi_tags=tags_metadata,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ══════════════════════════════════════════════════════════════
# 路由 — 密钥管理
# ══════════════════════════════════════════════════════════════

@app.post(
    "/key/register",
    tags=["密钥管理"],
    summary="注册/更新用户密钥",
    description="""
接收 App 端输入的 `secret_key`，服务端执行派生：
```
first    = MD5(secret_key)     # 16 bytes
second   = MD5(first)          # 16 bytes
aes_key  = first || second     # 32 bytes → AES-256
```
派生结果存入 `user_keys` 表。Consumer 端用同样规则本地派生，AES key 永不出现在网络请求中。
""",
)
async def register_key(body: KeyRegisterRequest, x_token: str = Header(...)):
    check_token(x_token)

    aes_key_b64 = derive_aes_key_b64(body.secret_key)
    session     = get_session()
    try:
        record = session.query(UserKey).filter(UserKey.user_id == body.user_id).first()
        if record:
            record.secret_key  = body.secret_key
            record.aes_key_b64 = aes_key_b64
            record.updated_at  = datetime.datetime.now()
            action = "updated"
        else:
            session.add(UserKey(
                user_id=body.user_id,
                secret_key=body.secret_key,
                aes_key_b64=aes_key_b64,
            ))
            action = "created"
        session.commit()
        log.info(f"密钥 {action}：user_id={body.user_id}")
        return {
            "user_id":         body.user_id,
            "status":          action,
            "aes_key_preview": aes_key_b64[:8] + "...",
        }
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"密钥存储失败：{e}")
    finally:
        session.close()


@app.post("/key/verify", tags=["密钥管理"], summary="验证用户密钥是否正确")
async def verify_key(body: KeyVerifyRequest, x_token: str = Header(...)):
    check_token(x_token)
    session = get_session()
    try:
        record = session.query(UserKey).filter(UserKey.user_id == body.user_id).first()
        if not record:
            raise HTTPException(status_code=404, detail=f"用户 {body.user_id} 未注册密钥")
        match = record.secret_key == body.secret_key
        return {"user_id": body.user_id, "match": match}
    finally:
        session.close()


@app.get("/key/{user_id}", tags=["密钥管理"], summary="查询用户密钥状态")
async def get_key_info(user_id: str, x_token: str = Header(...)):
    check_token(x_token)
    session = get_session()
    try:
        record = session.query(UserKey).filter(UserKey.user_id == user_id).first()
        if not record:
            raise HTTPException(status_code=404, detail=f"用户 {user_id} 未注册密钥")
        return {
            "user_id":         record.user_id,
            "registered":      True,
            "aes_key_preview": record.aes_key_b64[:8] + "...",
            "created_at":      record.created_at.isoformat() if record.created_at else None,
            "updated_at":      record.updated_at.isoformat() if record.updated_at else None,
        }
    finally:
        session.close()


@app.delete("/key/{user_id}", tags=["密钥管理"], summary="删除用户密钥")
async def delete_key(user_id: str, x_token: str = Header(...)):
    check_token(x_token)
    session = get_session()
    try:
        record = session.query(UserKey).filter(UserKey.user_id == user_id).first()
        if not record:
            raise HTTPException(status_code=404, detail=f"用户 {user_id} 未注册密钥")
        session.delete(record)
        session.commit()
        log.info(f"密钥已删除：user_id={user_id}")
        return {"user_id": user_id, "deleted": True}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"删除失败：{e}")
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════
# 路由 — 命令发送
# ══════════════════════════════════════════════════════════════

@app.post("/send", tags=["命令发送"], summary="提交单条命令")
async def send_command(body: SendRequest, x_token: str = Header(...)):
    check_token(x_token)

    session = get_session()
    try:
        get_user_aes_key_b64(body.user_id, session)

        scheduled = (
            datetime.datetime.fromisoformat(body.scheduled_at)
            if body.scheduled_at else None
        )
        task = CommandTask(
            id=str(uuid.uuid4()),
            user_id=body.user_id,
            command=body.command,
            timeout=body.timeout,
            reply_to=body.reply_to,
            max_retries=body.max_retries,
            scheduled_at=scheduled,
            status="pending",
        )
        session.add(task)
        session.commit()
        session.refresh(task)

        if not scheduled:
            try:
                cmd_id       = send_encrypted_command(task, session)
                task.status  = "sent"
                task.cmd_id  = cmd_id
                task.sent_at = datetime.datetime.now()
                session.commit()
            except Exception as e:
                task.retry_count += 1
                task.error_msg    = str(e)
                session.commit()
                log.error(f"立即发送失败，任务保留 pending 等待轮询重试：{e}")

        return {
            "id":           task.id,
            "cmd_id":       task.cmd_id,
            "user_id":      task.user_id,
            "command":      task.command,
            "status":       task.status,
            "scheduled_at": task.scheduled_at.isoformat() if task.scheduled_at else None,
            "created_at":   task.created_at.isoformat(),
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        session.close()


@app.post("/send/batch", tags=["命令发送"], summary="批量提交命令")
async def send_batch(body: BatchSendRequest, x_token: str = Header(...)):
    check_token(x_token)

    session = get_session()
    results = []
    try:
        for item in body.commands:
            scheduled = (
                datetime.datetime.fromisoformat(item.scheduled_at)
                if item.scheduled_at else None
            )
            task = CommandTask(
                id=str(uuid.uuid4()),
                user_id=item.user_id,
                command=item.command,
                timeout=item.timeout,
                reply_to=item.reply_to,
                max_retries=item.max_retries,
                scheduled_at=scheduled,
                status="pending",
            )
            session.add(task)
            session.flush()

            if not scheduled:
                try:
                    cmd_id       = send_encrypted_command(task, session)
                    task.status  = "sent"
                    task.cmd_id  = cmd_id
                    task.sent_at = datetime.datetime.now()
                    results.append({"id": task.id, "cmd_id": cmd_id, "status": "sent"})
                except Exception as e:
                    task.retry_count += 1
                    task.error_msg    = str(e)
                    results.append({"id": task.id, "status": f"error: {e}"})
            else:
                results.append({"id": task.id, "status": "scheduled"})

        session.commit()
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"批量提交失败：{e}")
    finally:
        session.close()

    return {"total": len(results), "results": results}


# ══════════════════════════════════════════════════════════════
# 路由 — 结果获取
# ══════════════════════════════════════════════════════════════

@app.get(
    "/result/{user_id}",
    tags=["结果获取"],
    summary="获取用户的命令执行结果",
    description="""
从 `result.{user_id}` 队列中获取命令执行结果，自动解密后返回并入库。

- `max_count`: 最多获取多少条消息（默认 10）
- `auto_ack`: 是否自动确认消息（默认 True，确认后消息从队列中删除）
- 结果会自动保存到 `t_command_results` 表
""",
)
async def get_results(
    user_id: str,
    max_count: int = 10,
    auto_ack: bool = True,
    x_token: str = Header(...),
):
    check_token(x_token)

    session = get_session()
    try:
        # 获取用户的 AES key
        key_b64 = get_user_aes_key_b64(user_id, session)

        # 从结果队列获取消息
        queue = f"result.{user_id}"
        messages = mq.get_messages(queue, max_count=max_count, auto_ack=auto_ack)

        results = []
        saved_count = 0
        for body, properties, delivery_tag in messages:
            try:
                # 解析加密载体
                envelope = json.loads(body.decode("utf-8"))

                # AES 解密
                plaintext = aes_decrypt(envelope, key_b64)

                # 解析结果 JSON
                result = json.loads(plaintext)
                result["_correlation_id"] = properties.correlation_id
                results.append(result)

                # 入库
                if save_result_to_db(user_id, result, session):
                    saved_count += 1

            except Exception as e:
                log.error(f"解密结果失败：{e}")
                results.append({
                    "_error": str(e),
                    "_correlation_id": properties.correlation_id if properties else None,
                    "_raw": body.decode("utf-8", errors="replace")[:200],
                })

        return {
            "user_id": user_id,
            "queue": queue,
            "count": len(results),
            "saved_count": saved_count,
            "results": results,
        }

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取结果失败：{e}")
    finally:
        session.close()


@app.get(
    "/result/{user_id}/history",
    tags=["结果获取"],
    summary="查询历史执行结果（从数据库）",
    description="""
从数据库中查询已入库的命令执行结果历史记录。
""",
)
async def get_result_history(
    user_id: str,
    limit: int = 20,
    offset: int = 0,
    cmd_id: Optional[str] = None,
    x_token: str = Header(...),
):
    check_token(x_token)

    session = get_session()
    try:
        q = session.query(CommandResult).filter(CommandResult.user_id == user_id)

        if cmd_id:
            q = q.filter(CommandResult.cmd_id == cmd_id)

        total = q.count()
        records = q.order_by(CommandResult.received_at.desc()).limit(limit).offset(offset).all()

        return {
            "user_id": user_id,
            "total": total,
            "limit": limit,
            "offset": offset,
            "results": [
                {
                    "id": r.id,
                    "cmd_id": r.cmd_id,
                    "stdout": r.stdout,
                    "stderr": r.stderr,
                    "returncode": r.returncode,
                    "duration_ms": r.duration_ms,
                    "cwd": r.cwd,
                    "received_at": r.received_at.isoformat() if r.received_at else None,
                }
                for r in records
            ],
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询历史结果失败：{e}")
    finally:
        session.close()


@app.get(
    "/result/{user_id}/peek",
    tags=["结果获取"],
    summary="预览结果队列（不消费）",
    description="""
预览 `result.{user_id}` 队列中的消息，不会从队列中删除消息。
用于检查是否有待处理的结果。
""",
)
async def peek_results(
    user_id: str,
    max_count: int = 10,
    x_token: str = Header(...),
):
    check_token(x_token)

    session = get_session()
    try:
        key_b64 = get_user_aes_key_b64(user_id, session)
        queue = f"result.{user_id}"

        # 获取消息但不确认（auto_ack=False）
        ch = mq.channel()
        ch.queue_declare(queue=queue, durable=True)

        results = []
        delivery_tags = []

        for _ in range(max_count):
            method, properties, body = ch.basic_get(queue=queue, auto_ack=False)
            if method is None:
                break

            delivery_tags.append(method.delivery_tag)

            try:
                envelope = json.loads(body.decode("utf-8"))
                plaintext = aes_decrypt(envelope, key_b64)
                result = json.loads(plaintext)
                result["_correlation_id"] = properties.correlation_id
                results.append(result)
            except Exception as e:
                results.append({
                    "_error": str(e),
                    "_correlation_id": properties.correlation_id if properties else None,
                })

        # 将所有消息重新放回队列
        for tag in delivery_tags:
            ch.basic_nack(delivery_tag=tag, requeue=True)

        return {
            "user_id": user_id,
            "queue": queue,
            "count": len(results),
            "results": results,
            "_note": "预览模式：消息未从队列中删除",
        }

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"预览结果失败：{e}")
    finally:
        session.close()


@app.get(
    "/result/{user_id}/{cmd_id}",
    tags=["结果获取"],
    summary="获取特定命令的执行结果",
    description="""
根据 `cmd_id`（correlation_id）从结果队列中查找特定命令的执行结果。

- 会扫描队列中的消息（最多 100 条），找到匹配的 cmd_id 后返回
- 不匹配的消息会被重新放回队列
- 如果找不到对应结果，返回 404
""",
)
async def get_result_by_cmd_id(
    user_id: str,
    cmd_id: str,
    x_token: str = Header(...),
):
    check_token(x_token)

    session = get_session()
    try:
        # 获取用户的 AES key
        key_b64 = get_user_aes_key_b64(user_id, session)

        # 从结果队列查找特定消息
        queue = f"result.{user_id}"
        message = mq.get_message_by_correlation_id(queue, cmd_id)

        if message is None:
            raise HTTPException(
                status_code=404,
                detail=f"未找到 cmd_id={cmd_id} 的执行结果（可能尚未执行完成或已被消费）"
            )

        body, properties = message
        try:
            # 解析加密载体
            envelope = json.loads(body.decode("utf-8"))

            # AES 解密
            plaintext = aes_decrypt(envelope, key_b64)

            # 解析结果 JSON
            result = json.loads(plaintext)
            result["_correlation_id"] = properties.correlation_id

            return {
                "user_id": user_id,
                "cmd_id": cmd_id,
                "queue": queue,
                "result": result,
            }

        except Exception as e:
            log.error(f"解密结果失败：{e}")
            raise HTTPException(status_code=500, detail=f"解密结果失败：{e}")

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取结果失败：{e}")
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════
# 路由 — 任务管理
# ══════════════════════════════════════════════════════════════

@app.get("/tasks", tags=["任务管理"], summary="查询任务列表")
async def list_tasks(
    status:  Optional[str] = None,
    user_id: Optional[str] = None,
    limit:   int = 20,
    offset:  int = 0,
    x_token: str = Header(...),
):
    check_token(x_token)
    session = get_session()
    try:
        q = session.query(CommandTask)
        if status:  q = q.filter(CommandTask.status  == status)
        if user_id: q = q.filter(CommandTask.user_id == user_id)
        total = q.count()
        tasks = q.order_by(CommandTask.created_at.desc()).limit(limit).offset(offset).all()
        return {
            "total":  total,
            "limit":  limit,
            "offset": offset,
            "tasks": [
                {
                    "id":           t.id,
                    "user_id":      t.user_id,
                    "command":      t.command,
                    "status":       t.status,
                    "cmd_id":       t.cmd_id,
                    "retry_count":  t.retry_count,
                    "error_msg":    t.error_msg,
                    "created_at":   t.created_at.isoformat()   if t.created_at   else None,
                    "sent_at":      t.sent_at.isoformat()      if t.sent_at      else None,
                    "scheduled_at": t.scheduled_at.isoformat() if t.scheduled_at else None,
                }
                for t in tasks
            ],
        }
    finally:
        session.close()


@app.get("/tasks/{task_id}", tags=["任务管理"], summary="查询单条任务")
async def get_task(task_id: str, x_token: str = Header(...)):
    check_token(x_token)
    session = get_session()
    try:
        task = session.query(CommandTask).filter(CommandTask.id == task_id).first()
        if not task:
            raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
        return {
            "id":           task.id,
            "user_id":      task.user_id,
            "command":      task.command,
            "timeout":      task.timeout,
            "status":       task.status,
            "cmd_id":       task.cmd_id,
            "retry_count":  task.retry_count,
            "max_retries":  task.max_retries,
            "error_msg":    task.error_msg,
            "created_at":   task.created_at.isoformat()   if task.created_at   else None,
            "sent_at":      task.sent_at.isoformat()      if task.sent_at      else None,
            "scheduled_at": task.scheduled_at.isoformat() if task.scheduled_at else None,
        }
    finally:
        session.close()


@app.post("/tasks/{task_id}/retry", tags=["任务管理"], summary="手动重试失败任务")
async def retry_task(task_id: str, x_token: str = Header(...)):
    check_token(x_token)
    session = get_session()
    try:
        task = session.query(CommandTask).filter(CommandTask.id == task_id).first()
        if not task:
            raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
        if task.status not in ("failed", "pending"):
            raise HTTPException(
                status_code=400,
                detail=f"只能重试 failed/pending 状态的任务，当前：{task.status}",
            )
        task.status      = "pending"
        task.retry_count = 0
        task.error_msg   = None
        session.commit()

        cmd_id       = send_encrypted_command(task, session)
        task.status  = "sent"
        task.cmd_id  = cmd_id
        task.sent_at = datetime.datetime.now()
        session.commit()
        return {"id": task_id, "cmd_id": cmd_id, "status": "sent"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"重试失败：{e}")
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════
# 路由 — 轮询控制
# ══════════════════════════════════════════════════════════════

@app.post("/poll/trigger", tags=["轮询控制"], summary="手动触发数据库轮询")
async def trigger_poll(x_token: str = Header(...)):
    check_token(x_token)
    import asyncio
    await asyncio.get_event_loop().run_in_executor(None, poll_and_send)
    return {"triggered": True, "time": datetime.datetime.now().isoformat()}


# ══════════════════════════════════════════════════════════════
# 路由 — 系统
# ══════════════════════════════════════════════════════════════

@app.get("/health", tags=["系统"], summary="健康检查")
async def health():
    mq_ok = db_ok = False
    try:
        mq.channel()
        mq_ok = True
    except Exception:
        pass
    try:
        with get_engine().connect() as conn:
            conn.close()
        db_ok = True
    except Exception:
        pass

    job = scheduler.get_job("db_poll")
    return {
        "status":        "ok" if (mq_ok and db_ok) else "degraded",
        "mq":            {"host": f"{MQ_HOST}:{MQ_PORT}", "ok": mq_ok},
        "db":            {"host": f"{DB_HOST}/{DB_NAME}", "ok": db_ok},
        "poll_interval": f"{POLL_INTERVAL}s",
        "next_poll":     job.next_run_time.isoformat() if job and job.next_run_time else None,
        "global_aes":    bool(AES_KEY_B64),
        "time":          datetime.datetime.now().isoformat(),
    }


@app.post("/gen-key", tags=["系统"], summary="生成随机 AES-256 密钥")
async def gen_key(x_token: str = Header(...)):
    check_token(x_token)
    key = os.urandom(32)
    b64 = base64.b64encode(key).decode()
    return {
        "key_base64": b64,
        "usage":      f'export AES_KEY="{b64}"',
    }


# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    import asyncio

    if "--init-db" in sys.argv:
        init_db()
        sys.exit(0)

    if not AES_KEY_B64:
        log.info("提示：未配置全局 AES_KEY，所有用户需先调用 POST /key/register 注册专属密钥")

    config = uvicorn.Config(app, host=API_HOST, port=API_PORT, log_level="warning")
    server = uvicorn.Server(config)
    asyncio.run(server.serve())
