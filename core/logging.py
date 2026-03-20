"""
结构化日志系统
- 控制台：彩色人类可读格式
- 文件：JSON 格式，按天轮转，保留 30 天
"""
import logging
import logging.handlers
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


LOG_DIR = Path("logs")


class JsonFormatter(logging.Formatter):
    """JSON 格式化器，用于文件日志"""

    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
            "level":     record.levelname,
            "logger":    record.name,
            "message":   record.getMessage(),
            "module":    record.module,
            "func":      record.funcName,
            "line":      record.lineno,
        }
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        if hasattr(record, "extra"):
            log_obj.update(record.extra)
        return json.dumps(log_obj, ensure_ascii=False)


class ColorFormatter(logging.Formatter):
    """彩色控制台格式化器"""

    COLORS = {
        "DEBUG":    "\033[36m",   # cyan
        "INFO":     "\033[32m",   # green
        "WARNING":  "\033[33m",   # yellow
        "ERROR":    "\033[31m",   # red
        "CRITICAL": "\033[35m",   # magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        ts    = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        msg   = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        return f"{color}[{ts}] [{record.levelname:<8}] [{record.name}] {msg}{self.RESET}"


def setup_logging(
    name:      str,
    level:     str = "INFO",
    log_file:  Optional[str] = None,
    json_logs: bool = False,
) -> logging.Logger:
    """
    初始化并返回一个命名 logger

    Args:
        name:      logger 名称（通常传 __name__）
        level:     日志级别（DEBUG/INFO/WARNING/ERROR）
        log_file:  日志文件名（None=不写文件），文件存放在 logs/ 目录
        json_logs: 是否使用 JSON 格式（生产环境建议 True）
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    # 控制台 handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        JsonFormatter() if json_logs else ColorFormatter()
    )
    logger.addHandler(console_handler)

    # 文件 handler（按天轮转）
    if log_file:
        LOG_DIR.mkdir(exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            filename=LOG_DIR / log_file,
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8",
        )
        file_handler.setFormatter(JsonFormatter())
        logger.addHandler(file_handler)

    logger.propagate = False
    return logger


def get_logger(name: str) -> logging.Logger:
    """快捷获取 logger（已初始化后使用）"""
    return logging.getLogger(name)
