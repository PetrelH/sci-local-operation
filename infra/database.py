"""
数据库层
SQLAlchemy ORM + 连接池管理
"""
import uuid
import datetime
from typing import Optional, Generator
from contextlib import contextmanager

from sqlalchemy import (
    create_engine, Column, String, Integer, Text, DateTime, Enum
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from core.config import DBSettings
from core.exceptions import DBError
from core.logging import get_logger

log = get_logger(__name__)
Base = declarative_base()


# ── ORM 模型 ───────────────────────────────────────────────────

class CommandTask(Base):
    """命令任务表"""
    __tablename__ = "command_tasks"

    id           = Column(String(36),  primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id      = Column(String(64),  nullable=False,  index=True,   comment="目标用户ID")
    command      = Column(Text,        nullable=False,                comment="Shell 命令")
    timeout      = Column(Integer,     default=30,                    comment="超时秒数")
    reply_to     = Column(String(128), nullable=True,                 comment="结果回写队列")
    status       = Column(
        Enum("pending", "sent", "failed", name="task_status"),
        default="pending", index=True,                               comment="任务状态"
    )
    cmd_id       = Column(String(36),  nullable=True,                 comment="MQ 消息 ID")
    retry_count  = Column(Integer,     default=0,                     comment="已重试次数")
    max_retries  = Column(Integer,     default=3,                     comment="最大重试次数")
    error_msg    = Column(Text,        nullable=True,                 comment="失败原因")
    created_at   = Column(DateTime,    default=datetime.datetime.now, comment="创建时间")
    sent_at      = Column(DateTime,    nullable=True,                 comment="发送时间")
    scheduled_at = Column(DateTime,    nullable=True, index=True,     comment="计划执行时间")


# ── 数据库客户端 ───────────────────────────────────────────────

class Database:
    """数据库连接管理器"""

    def __init__(self, settings: DBSettings):
        self._settings = settings
        self._engine   = None
        self._Session  = None

    def init(self) -> None:
        """初始化连接池，创建表"""
        try:
            self._engine = create_engine(
                self._settings.url,
                pool_size=self._settings.pool_size,
                max_overflow=self._settings.max_overflow,
                pool_pre_ping=True,
                pool_recycle=self._settings.pool_recycle,
            )
            Base.metadata.create_all(self._engine)
            self._Session = sessionmaker(bind=self._engine)
            log.info(f"数据库连接成功 {self._settings.host}/{self._settings.name}")
        except Exception as e:
            raise DBError(str(e))

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        """获取数据库 session（上下文管理器，自动提交/回滚）"""
        if self._Session is None:
            raise DBError("数据库未初始化，请先调用 init()")
        db = self._Session()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def health_check(self) -> bool:
        """检查数据库连接是否正常"""
        try:
            with self._engine.connect() as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def dispose(self) -> None:
        """关闭连接池"""
        if self._engine:
            self._engine.dispose()
            log.info("数据库连接池已关闭")
