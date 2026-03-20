"""
MQ Consumer
从 agent.{user_id} 队列消费加密命令，执行后将结果加密回写
"""
import datetime
import json
import os
import subprocess
import sys

import pika

from core.config import get_settings
from core.crypto import encrypt, decrypt
from core.exceptions import CommandBlockedError, CryptoError
from core.logging import setup_logging

settings = get_settings()

USER_ID = os.getenv("MQ_USER_ID", "")
if not USER_ID:
    print("错误：环境变量 MQ_USER_ID 未设置")
    sys.exit(1)

log = setup_logging(
    name=f"consumer.{USER_ID}",
    level="INFO",
    log_file=f"consumer_{USER_ID}.log",
)

QUEUE_CMD    = f"agent.{USER_ID}"
QUEUE_RESULT = f"result.{USER_ID}"
BLOCKED      = ["rm -rf /", "rm -rf ~", ":(){ :|:& };:", "mkfs", "dd if="]
ENCODING     = "utf-8"

_cwd = os.path.expanduser("~")


def execute(command: str, timeout: int = 30) -> dict:
    """本地执行 Shell 命令"""
    global _cwd

    # 处理 cd
    s = command.strip()
    if s.startswith("cd ") and not any(c in s for c in (";", "|", "&&")):
        target   = s[3:].strip().strip('"').strip("'")
        expanded = os.path.expanduser(target)
        if not os.path.isabs(expanded):
            expanded = os.path.normpath(os.path.join(_cwd, expanded))
        if os.path.isdir(expanded):
            _cwd = expanded
            return {"stdout": f"cwd: {_cwd}", "stderr": "", "returncode": 0, "cwd": _cwd}
        return {"stdout": "", "stderr": f"cd: no such directory: {expanded}", "returncode": 1, "cwd": _cwd}

    safe = _cwd.replace("'", "'\\''")
    start = datetime.datetime.now()
    try:
        proc = subprocess.run(
            f"cd '{safe}' && {command}",
            shell=True, executable="/bin/zsh",
            capture_output=True, timeout=timeout,
            encoding=ENCODING, errors="replace",
        )
        duration_ms = int((datetime.datetime.now() - start).total_seconds() * 1000)
        return {
            "stdout": proc.stdout, "stderr": proc.stderr,
            "returncode": proc.returncode, "duration_ms": duration_ms, "cwd": _cwd,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"超时（>{timeout}s）", "returncode": 124, "cwd": _cwd}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": 1, "cwd": _cwd}


def on_message(ch, method, properties, body):
    """MQ 消息回调"""
    try:
        envelope = json.loads(body.decode("utf-8"))

        # 解密
        try:
            plaintext = decrypt(envelope, settings.crypto.aes_key)
            msg       = json.loads(plaintext)
        except (CryptoError, Exception) as e:
            log.error(f"解密失败：{e}")
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return

        cmd_id   = msg.get("cmd_id", "")
        command  = msg.get("command", "").strip()
        timeout  = msg.get("timeout", 30)
        reply_to = msg.get("reply_to") or properties.reply_to or QUEUE_RESULT

        log.info(f"[{cmd_id}] 执行：{command!r}")

        # 黑名单检查
        low = command.lower()
        if any(kw in low for kw in BLOCKED):
            result = {"cmd_id": cmd_id, "stdout": "", "stderr": "命令被安全策略拒绝", "returncode": 403}
            log.warning(f"[{cmd_id}] 被黑名单拦截")
        elif not command:
            result = {"cmd_id": cmd_id, "stdout": "", "stderr": "命令为空", "returncode": 1}
        else:
            exec_result = execute(command, timeout)
            result = {"cmd_id": cmd_id, **exec_result}
            log.info(f"[{cmd_id}] 完成 rc={result.get('returncode')} {result.get('duration_ms', 0)}ms")

        # 加密结果并回写
        encrypted = encrypt(json.dumps(result, ensure_ascii=False), settings.crypto.aes_key)
        ch.queue_declare(queue=reply_to, durable=True)
        ch.basic_publish(
            exchange="",
            routing_key=reply_to,
            body=json.dumps(encrypted, ensure_ascii=False).encode("utf-8"),
            properties=pika.BasicProperties(
                delivery_mode=2,
                correlation_id=cmd_id,
                content_type="application/json",
            ),
        )
        log.info(f"[{cmd_id}] 结果已回写到 {reply_to}")
        ch.basic_ack(delivery_tag=method.delivery_tag)

    except Exception as e:
        log.error(f"消息处理异常：{e}", exc_info=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


def start():
    from infra.mq import MQClient
    client = MQClient(settings.mq)
    log.info(f"Consumer 启动 user_id={USER_ID} queue={QUEUE_CMD}")
    client.consume(queue=QUEUE_CMD, callback=on_message)


if __name__ == "__main__":
    start()
