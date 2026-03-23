"""
Shell Agent — 统一配置文件
所有模块从此处读取配置，支持通过环境变量覆盖默认值。
"""

import os

# ══════════════════════════════════════════════════════════════
# Agent HTTP 服务配置（agent.py / menubar_app.py）
# ══════════════════════════════════════════════════════════════

AGENT_TOKEN           = os.getenv("AGENT_TOKEN",  "my-secret-token")
AGENT_HOST            = os.getenv("AGENT_HOST",   "0.0.0.0")
AGENT_PORT            = int(os.getenv("AGENT_PORT", "8000"))

# 命令输出编码
OUTPUT_ENCODING       = "utf-8"

# 黑名单关键词（禁止执行）
BLOCKED_KEYWORDS: list[str] = [
    "rm -rf /",
    "rm -rf ~",
    ":(){ :|:& };:",
    "mkfs",
    "dd if=",
]

# ══════════════════════════════════════════════════════════════
# RabbitMQ 配置（mq_consumer.py / mq_producer_api.py / mq_sender.py）
# ══════════════════════════════════════════════════════════════

MQ_HOST   = os.getenv("MQ_HOST",  "10.17.1.17")
MQ_PORT   = int(os.getenv("MQ_PORT", "5672"))
MQ_USER   = os.getenv("MQ_USER",  "admin")
MQ_PASS   = os.getenv("MQ_PASS",  "admin123")
MQ_VHOST  = os.getenv("MQ_VHOST", "/")

# ══════════════════════════════════════════════════════════════
# 密钥配置（mq_consumer.py / mq_producer_api.py / mq_sender.py）
# ══════════════════════════════════════════════════════════════

# 推荐：原始密钥，两端相同即可，AES key 由程序本地派生
SECRET_KEY  = os.getenv("SECRET_KEY", "")

# 兼容旧版：直接传 base64 编码的 32 字节 AES-256 key
AES_KEY_B64 = os.getenv("AES_KEY",    "")

# ══════════════════════════════════════════════════════════════
# MQ Consumer 配置（mq_consumer.py）
# ══════════════════════════════════════════════════════════════

MQ_USER_ID = os.getenv("MQ_USER_ID", "")

# ══════════════════════════════════════════════════════════════
# MQ Producer API 配置（mq_producer_api.py）
# ══════════════════════════════════════════════════════════════

# MySQL 数据库
DB_HOST = os.getenv("DB_HOST",  "10.17.1.17")
DB_PORT = int(os.getenv("DB_PORT", "6306"))
DB_USER = os.getenv("DB_USER",  "root")
DB_PASS = os.getenv("DB_PASS",  "123456")
DB_NAME = os.getenv("DB_NAME",  "sci_operation_local")

# Producer API 服务
API_TOKEN = os.getenv("API_TOKEN",    "producer-secret")
API_HOST  = os.getenv("API_HOST",     "0.0.0.0")
API_PORT  = int(os.getenv("API_PORT", "9000"))

# 数据库轮询间隔（秒），默认 5 分钟
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))

# ══════════════════════════════════════════════════════════════
# 打包配置（build_pkg.py）
# ══════════════════════════════════════════════════════════════

APP_NAME       = "ShellAgent"
PKG_IDENTIFIER = "com.shellagent.agent"
PKG_VERSION    = "1.0.0"
MIN_MACOS      = "12.0"

# ══════════════════════════════════════════════════════════════
# 菜单栏 App 配置（menubar_app.py）
# ══════════════════════════════════════════════════════════════

MENUBAR_LABEL    = "com.shellagent"
MENUBAR_PLIST    = "/Library/LaunchDaemons/com.shellagent.plist"
MENUBAR_BIN      = "/usr/local/bin/shellagent"
MENUBAR_LOG      = "/var/log/shellagent.log"
MENUBAR_WEB_DIR  = "/usr/local/share/shellagent/console.html"

# 状态轮询间隔（秒）
MENUBAR_POLL_INTERVAL = 5
