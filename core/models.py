"""
共享数据模型
跨服务共用的 Pydantic schema 定义
"""
from typing import Optional
from pydantic import BaseModel, Field


# ── MQ 消息格式 ────────────────────────────────────────────────

class EncryptedPayload(BaseModel):
    """AES 加密消息体"""
    iv:   str = Field(..., description="base64 编码的 IV（16 字节）")
    data: str = Field(..., description="base64 编码的密文")


class CommandMessage(BaseModel):
    """命令消息（加密前的明文结构）"""
    cmd_id:   str = Field(...,  description="命令唯一 ID（UUID）")
    command:  str = Field(...,  description="Shell 命令")
    timeout:  int = Field(30,   description="超时秒数", ge=1, le=300)
    reply_to: str = Field(...,  description="结果回写队列")


class ResultMessage(BaseModel):
    """命令执行结果（加密前的明文结构）"""
    cmd_id:      str           = Field(..., description="对应的命令 ID")
    stdout:      str           = Field("",  description="标准输出")
    stderr:      str           = Field("",  description="标准错误")
    returncode:  int           = Field(..., description="退出码")
    duration_ms: Optional[int] = Field(None, description="执行耗时（毫秒）")
    cwd:         Optional[str] = Field(None, description="执行时的工作目录")
    sudo_used:   bool          = Field(False, description="是否使用了 sudo")


# ── API 请求/响应 ──────────────────────────────────────────────

class ErrorResponse(BaseModel):
    """统一错误响应"""
    code:    str           = Field(..., description="错误码")
    message: str           = Field(..., description="错误信息")
    detail:  Optional[str] = Field(None, description="详细信息")

    model_config = {
        "json_schema_extra": {
            "example": {
                "code":    "AUTH_ERROR",
                "message": "认证失败",
                "detail":  None,
            }
        }
    }


class HealthResponse(BaseModel):
    """健康检查响应"""
    status:  str  = Field(..., description="ok | degraded")
    version: str  = Field(..., description="服务版本")
    time:    str  = Field(..., description="当前时间 ISO8601")
