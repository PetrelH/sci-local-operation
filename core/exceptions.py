"""
统一异常体系
所有业务异常继承自 AppError，携带 code 和 HTTP status
"""
from typing import Optional


class AppError(Exception):
    """应用基础异常"""
    code:    str = "APP_ERROR"
    status:  int = 500

    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.detail  = detail

    def to_dict(self) -> dict:
        return {
            "code":    self.code,
            "message": self.message,
            "detail":  self.detail,
        }


# ── 认证 & 授权 ────────────────────────────────────────────────

class AuthError(AppError):
    code   = "AUTH_ERROR"
    status = 401

    def __init__(self, message: str = "认证失败"):
        super().__init__(message)


class ForbiddenError(AppError):
    code   = "FORBIDDEN"
    status = 403

    def __init__(self, message: str = "无权执行此操作"):
        super().__init__(message)


# ── 命令执行 ───────────────────────────────────────────────────

class CommandBlockedError(AppError):
    """命令被黑名单拦截"""
    code   = "COMMAND_BLOCKED"
    status = 403

    def __init__(self, keyword: str):
        super().__init__(f"命令包含被禁止的关键字：{keyword}", detail=keyword)


class CommandTimeoutError(AppError):
    """命令执行超时"""
    code   = "COMMAND_TIMEOUT"
    status = 408

    def __init__(self, timeout: int):
        super().__init__(f"命令执行超时（>{timeout}s）", detail=str(timeout))


class PermissionError(AppError):
    """命令权限不足"""
    code   = "PERMISSION_DENIED"
    status = 403

    def __init__(self, message: str = "权限不足（Operation not permitted）"):
        super().__init__(message)


class DirectoryNotFoundError(AppError):
    """目录不存在"""
    code   = "DIRECTORY_NOT_FOUND"
    status = 400

    def __init__(self, path: str):
        super().__init__(f"目录不存在：{path}", detail=path)


# ── 加密 ───────────────────────────────────────────────────────

class CryptoError(AppError):
    """加解密失败"""
    code   = "CRYPTO_ERROR"
    status = 500

    def __init__(self, message: str = "加解密操作失败"):
        super().__init__(message)


class InvalidKeyError(CryptoError):
    """AES 密钥无效"""
    code = "INVALID_KEY"

    def __init__(self, detail: str = ""):
        super().__init__(f"AES_KEY 无效：{detail}")


# ── 基础设施 ───────────────────────────────────────────────────

class MQConnectionError(AppError):
    """RabbitMQ 连接失败"""
    code   = "MQ_CONNECTION_ERROR"
    status = 502

    def __init__(self, detail: str = ""):
        super().__init__("RabbitMQ 连接失败", detail=detail)


class MQPublishError(AppError):
    """消息发布失败"""
    code   = "MQ_PUBLISH_ERROR"
    status = 502

    def __init__(self, detail: str = ""):
        super().__init__("消息发布失败", detail=detail)


class DBError(AppError):
    """数据库操作失败"""
    code   = "DB_ERROR"
    status = 500

    def __init__(self, detail: str = ""):
        super().__init__("数据库操作失败", detail=detail)


# ── 业务逻辑 ───────────────────────────────────────────────────

class TaskNotFoundError(AppError):
    """任务不存在"""
    code   = "TASK_NOT_FOUND"
    status = 404

    def __init__(self, task_id: str):
        super().__init__(f"任务不存在：{task_id}", detail=task_id)


class TaskStateError(AppError):
    """任务状态不允许此操作"""
    code   = "TASK_STATE_ERROR"
    status = 400

    def __init__(self, current: str, allowed: list[str]):
        super().__init__(
            f"当前状态 {current!r} 不允许此操作，允许状态：{allowed}",
            detail=current,
        )
