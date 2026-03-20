"""
统一配置管理
所有配置从环境变量读取，支持 .env 文件
"""
from functools import lru_cache
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    """本地 Shell Agent 服务配置"""
    token:    str = "my-secret-token"
    host:     str = "0.0.0.0"
    port:     int = 8000
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")


class MQSettings(BaseSettings):
    """RabbitMQ 连接配置"""
    host:      str = "localhost"
    port:      int = 5672
    user:      str = "guest"
    password:  str = "guest"
    vhost:     str = "/"
    heartbeat: int = 60

    model_config = SettingsConfigDict(env_prefix="MQ_", env_file=".env", extra="ignore")

    @property
    def url(self) -> str:
        return f"amqp://{self.user}:{self.password}@{self.host}:{self.port}{self.vhost}"


class DBSettings(BaseSettings):
    """MySQL 数据库配置"""
    host:     str = "localhost"
    port:     int = 3306
    user:     str = "root"
    password: str = ""
    name:     str = "shellagent"
    pool_size:     int = 5
    max_overflow:  int = 10
    pool_recycle:  int = 3600

    model_config = SettingsConfigDict(env_prefix="DB_", env_file=".env", extra="ignore")

    @property
    def url(self) -> str:
        return (
            f"mysql+pymysql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.name}?charset=utf8mb4"
        )


class ProducerSettings(BaseSettings):
    """MQ Producer API 配置"""
    api_token:     str = "producer-secret"
    api_host:      str = "0.0.0.0"
    api_port:      int = 9000
    poll_interval: int = 300   # 数据库轮询间隔（秒）
    log_level:     str = "INFO"

    model_config = SettingsConfigDict(env_prefix="PRODUCER_", env_file=".env", extra="ignore")


class CryptoSettings(BaseSettings):
    """AES 加密配置"""
    aes_key: str = ""   # base64 编码的 32 字节密钥，必填

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


class Settings(BaseSettings):
    """全局聚合配置"""
    env:  str = "development"   # development | staging | production
    timezone: str = "Asia/Shanghai"

    agent:    AgentSettings    = AgentSettings()
    mq:       MQSettings       = MQSettings()
    db:       DBSettings       = DBSettings()
    producer: ProducerSettings = ProducerSettings()
    crypto:   CryptoSettings   = CryptoSettings()

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    """获取全局配置（单例，缓存）"""
    return Settings()
