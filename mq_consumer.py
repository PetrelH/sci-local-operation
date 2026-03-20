"""
Shell Agent — RabbitMQ 消费者模块
从 agent.{user_id} queue 拉取加密命令，执行后将加密结果回写 reply_to queue

依赖：
    pip install pika cryptography pycryptodome

用法：
    python3 mq_consumer.py

环境变量：
    MQ_HOST        RabbitMQ host         (默认 localhost)
    MQ_PORT        RabbitMQ port         (默认 5672)
    MQ_USER        RabbitMQ username     (默认 guest)
    MQ_PASS        RabbitMQ password     (默认 guest)
    MQ_VHOST       RabbitMQ vhost        (默认 /)
    MQ_USER_ID     当前 agent 的用户ID   (必填)
    AES_KEY        AES-256 密钥 base64   (必填，32字节base64)
    AGENT_TOKEN    HTTP Agent token      (用于调用本地 exec 接口)
    AGENT_PORT     HTTP Agent port       (默认 8000)
"""

import os
import json
import base64
import logging
import time
import uuid
import subprocess
import datetime
import threading

import pika
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend

# ─── 日志 ────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mq_consumer")

# ─── 配置 ────────────────────────────────────────────────────
MQ_HOST    = os.getenv("MQ_HOST",    "localhost")
MQ_PORT    = int(os.getenv("MQ_PORT", "5672"))
MQ_USER    = os.getenv("MQ_USER",    "guest")
MQ_PASS    = os.getenv("MQ_PASS",    "guest")
MQ_VHOST   = os.getenv("MQ_VHOST",  "/")
USER_ID    = os.getenv("MQ_USER_ID", "")
AES_KEY_B64 = os.getenv("AES_KEY",  "")
AGENT_PORT = int(os.getenv("AGENT_PORT", "8000"))
AGENT_TOKEN = os.getenv("AGENT_TOKEN", "my-secret-token")

# queue 命名规则
QUEUE_CMD    = f"agent.{USER_ID}"        # 消费：接收命令
QUEUE_RESULT = f"result.{USER_ID}"      # 可选：默认结果 queue（message 可覆盖）

OUTPUT_ENCODING = "utf-8"

# ─── AES-256-CBC 加解密 ───────────────────────────────────────

def _load_aes_key() -> bytes:
    if not AES_KEY_B64:
        raise ValueError("环境变量 AES_KEY 未设置，请设置 32 字节的 base64 编码密钥")
    key = base64.b64decode(AES_KEY_B64)
    if len(key) != 32:
        raise ValueError(f"AES_KEY 解码后必须为 32 字节，当前为 {len(key)} 字节")
    return key


AES_KEY_BYTES: bytes = _load_aes_key() if AES_KEY_B64 else b""


def aes_encrypt(plaintext: str) -> dict:
    """
    AES-256-CBC 加密
    返回：{"iv": b64, "data": b64}
    iv  = 随机 16 字节（CBC 初始向量）
    data = PKCS7 填充后的密文
    """
    iv = os.urandom(16)                           # CBC 需要 128-bit IV
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    cipher = Cipher(algorithms.AES(AES_KEY_BYTES), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    ct = encryptor.update(padded) + encryptor.finalize()
    return {
        "iv":   base64.b64encode(iv).decode(),
        "data": base64.b64encode(ct).decode(),
    }


def aes_decrypt(payload: dict) -> str:
    """
    AES-256-CBC 解密
    payload: {"iv": b64, "data": b64}
    """
    iv = base64.b64decode(payload["iv"])
    ct = base64.b64decode(payload["data"])
    cipher = Cipher(algorithms.AES(AES_KEY_BYTES), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ct) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    return plaintext.decode("utf-8")


# ─── 命令执行 ─────────────────────────────────────────────────

_cwd = os.path.expanduser("~")   # 持久工作目录（与 agent.py 保持一致）


def execute_command(command: str, timeout: int = 30) -> dict:
    """在本地执行 shell 命令，返回结构化结果"""
    global _cwd
    start = datetime.datetime.now()

    # 处理 cd 命令
    stripped = command.strip()
    if stripped.startswith("cd ") and ";" not in stripped and "|" not in stripped:
        target = stripped[3:].strip().strip('"').strip("'")
        expanded = os.path.expanduser(target)
        if not os.path.isabs(expanded):
            expanded = os.path.normpath(os.path.join(_cwd, expanded))
        if os.path.isdir(expanded):
            _cwd = expanded
            return {"stdout": f"cwd: {_cwd}", "stderr": "", "returncode": 0}
        return {"stdout": "", "stderr": f"cd: no such directory: {expanded}", "returncode": 1}

    safe_cwd = _cwd.replace("'", "'\\''")
    actual_cmd = f"cd '{safe_cwd}' && {command}"

    try:
        proc = subprocess.run(
            actual_cmd,
            shell=True,
            executable="/bin/zsh",
            capture_output=True,
            timeout=timeout,
            encoding=OUTPUT_ENCODING,
            errors="replace",
        )
        duration_ms = int((datetime.datetime.now() - start).total_seconds() * 1000)
        return {
            "stdout":      proc.stdout,
            "stderr":      proc.stderr,
            "returncode":  proc.returncode,
            "duration_ms": duration_ms,
            "cwd":         _cwd,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"命令超时（>{timeout}s）", "returncode": 124, "duration_ms": timeout * 1000}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": 1, "duration_ms": 0}


# ─── MQ 消息处理 ──────────────────────────────────────────────

def on_message(channel, method, properties, body, connection):
    """收到 MQ 消息的回调"""
    try:
        # 1. 解析外层 JSON
        envelope = json.loads(body.decode("utf-8"))
        log.info(f"收到消息，开始解密...")

        # 2. AES 解密
        try:
            plaintext = aes_decrypt(envelope)
        except Exception as e:
            log.error(f"AES 解密失败：{e}")
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        # 3. 解析命令
        try:
            msg = json.loads(plaintext)
        except json.JSONDecodeError as e:
            log.error(f"解密后 JSON 解析失败：{e}")
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        cmd_id   = msg.get("cmd_id", str(uuid.uuid4()))
        command  = msg.get("command", "").strip()
        timeout  = msg.get("timeout", 30)
        reply_to = msg.get("reply_to") or properties.reply_to or QUEUE_RESULT

        log.info(f"[{cmd_id}] 执行命令：{command!r}  reply_to={reply_to}")

        # 4. 安全检查（黑名单）
        BLOCKED = ["rm -rf /", "rm -rf ~", ":(){ :|:& };:", "mkfs", "dd if="]
        if any(kw in command.lower() for kw in BLOCKED):
            result = {"cmd_id": cmd_id, "stdout": "", "stderr": "命令被安全策略拒绝", "returncode": 403}
            log.warning(f"[{cmd_id}] 命令被黑名单拦截")
        elif not command:
            result = {"cmd_id": cmd_id, "stdout": "", "stderr": "命令为空", "returncode": 1}
        else:
            # 5. 执行命令
            exec_result = execute_command(command, timeout)
            result = {"cmd_id": cmd_id, **exec_result}
            log.info(f"[{cmd_id}] 完成 rc={result.get('returncode')} duration={result.get('duration_ms')}ms")

        # 6. 加密结果
        encrypted_result = aes_encrypt(json.dumps(result, ensure_ascii=False))

        # 7. 回写结果到 reply_to queue
        if reply_to:
            try:
                # 确保 reply_to queue 存在
                channel.queue_declare(queue=reply_to, durable=True)
                channel.basic_publish(
                    exchange="",
                    routing_key=reply_to,
                    body=json.dumps(encrypted_result, ensure_ascii=False).encode("utf-8"),
                    properties=pika.BasicProperties(
                        delivery_mode=2,           # 持久化
                        correlation_id=cmd_id,     # 便于客户端匹配
                        content_type="application/json",
                    ),
                )
                log.info(f"[{cmd_id}] 结果已加密回写到 {reply_to}")
            except Exception as e:
                log.error(f"[{cmd_id}] 回写结果失败：{e}")

        # 8. ACK
        channel.basic_ack(delivery_tag=method.delivery_tag)

    except Exception as e:
        log.error(f"消息处理异常：{e}", exc_info=True)
        # NACK，requeue=False 避免毒消息死循环
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


# ─── 连接与消费（含自动重连）────────────────────────────────

def connect() -> pika.BlockingConnection:
    credentials = pika.PlainCredentials(MQ_USER, MQ_PASS)
    params = pika.ConnectionParameters(
        host=MQ_HOST,
        port=MQ_PORT,
        virtual_host=MQ_VHOST,
        credentials=credentials,
        heartbeat=60,
        blocked_connection_timeout=30,
    )
    return pika.BlockingConnection(params)


def start_consumer():
    retry_delay = 5
    while True:
        try:
            log.info(f"连接 RabbitMQ {MQ_HOST}:{MQ_PORT} vhost={MQ_VHOST}...")
            conn = connect()
            channel = conn.channel()

            # 声明 queue（幂等，不存在则创建）
            channel.queue_declare(queue=QUEUE_CMD, durable=True)
            channel.queue_declare(queue=QUEUE_RESULT, durable=True)

            # 每次只取一条消息，处理完再取下一条
            channel.basic_qos(prefetch_count=1)

            channel.basic_consume(
                queue=QUEUE_CMD,
                on_message_callback=lambda ch, method, props, body: on_message(ch, method, props, body, conn),
            )

            log.info(f"✅  开始监听队列：{QUEUE_CMD}")
            retry_delay = 5   # 重置重试间隔
            channel.start_consuming()

        except pika.exceptions.AMQPConnectionError as e:
            log.error(f"RabbitMQ 连接断开：{e}，{retry_delay}s 后重试...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)   # 指数退避，最长 60s
        except KeyboardInterrupt:
            log.info("收到中断信号，停止消费")
            break
        except Exception as e:
            log.error(f"未知错误：{e}，{retry_delay}s 后重试...", exc_info=True)
            time.sleep(retry_delay)


# ─── 工具：生成 AES 密钥 ──────────────────────────────────────

def generate_key():
    """生成一个新的 AES-256 密钥并打印 base64 编码"""
    key = os.urandom(32)
    print("新生成的 AES-256 密钥（base64）：")
    print(base64.b64encode(key).decode())
    print("\n设置环境变量：")
    print(f'export AES_KEY="{base64.b64encode(key).decode()}"')


# ─── 入口 ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if "--gen-key" in sys.argv:
        generate_key()
        sys.exit(0)

    if not USER_ID:
        log.error("环境变量 MQ_USER_ID 未设置，请指定当前用户ID")
        sys.exit(1)

    if not AES_KEY_B64:
        log.error("环境变量 AES_KEY 未设置，运行 python3 mq_consumer.py --gen-key 生成密钥")
        sys.exit(1)

    log.info(f"Shell Agent MQ Consumer 启动")
    log.info(f"用户ID：{USER_ID}  队列：{QUEUE_CMD}")
    log.info(f"RabbitMQ：{MQ_HOST}:{MQ_PORT}")

    start_consumer()
