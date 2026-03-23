"""
Shell Agent — RabbitMQ 消费者模块
从 agent.{user_id} queue 拉取加密命令，执行后将加密结果回写 reply_to queue

密钥派生规则（与 Producer 端一致）：
    aes_key = MD5(SECRET_KEY) || MD5(MD5(SECRET_KEY))   →  32 bytes (AES-256)
    两端只要 SECRET_KEY 相同，派生结果必然一致，AES key 永不通过网络传输。

依赖：
    pip install pika cryptography

用法：
    python3 mq_consumer.py
"""

import os
import json
import base64
import hashlib
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

from config import (
    MQ_HOST,
    MQ_PORT,
    MQ_USER,
    MQ_PASS,
    MQ_VHOST,
    MQ_USER_ID     as USER_ID,
    SECRET_KEY,
    AES_KEY_B64,
    AGENT_PORT,
    AGENT_TOKEN,
    OUTPUT_ENCODING,
    BLOCKED_KEYWORDS,
)

# ─── 日志 ────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mq_consumer")

# queue 命名规则
QUEUE_CMD    = f"agent.{USER_ID}"   # 消费：接收命令
QUEUE_RESULT = f"result.{USER_ID}"  # 默认回写队列


# ─── 密钥派生（与 Producer 端完全一致）───────────────────────

def derive_aes_key(secret_key: str) -> bytes:
    """
    派生规则：
      first   = MD5(secret_key)   → 16 bytes
      second  = MD5(first)        → 16 bytes
      aes_key = first || second   → 32 bytes（AES-256）
    """
    first  = hashlib.md5(secret_key.encode("utf-8")).digest()
    second = hashlib.md5(first).digest()
    return first + second


def _resolve_aes_key() -> bytes:
    """
    密钥解析优先级：
      1. SECRET_KEY  → 本地派生（推荐）
      2. AES_KEY_B64 → 直接 base64 解码（兼容旧版）
      3. 两者都未设置 → 报错退出
    """
    if SECRET_KEY:
        key     = derive_aes_key(SECRET_KEY)
        preview = base64.b64encode(key).decode()[:8]
        log.info(f"使用 SECRET_KEY 本地派生 AES-256 key（preview: {preview}...）")
        return key

    if AES_KEY_B64:
        key = base64.b64decode(AES_KEY_B64)
        if len(key) != 32:
            raise ValueError(
                f"AES_KEY 解码后须为 32 字节，当前 {len(key)} 字节。"
                "建议改用 SECRET_KEY 环境变量，由程序自动派生正确长度的 key。"
            )
        log.info("使用环境变量 AES_KEY（base64）作为 AES-256 key")
        return key

    raise ValueError(
        "未设置密钥环境变量！请设置 SECRET_KEY（推荐）或 AES_KEY（兼容旧版）。\n"
        "  SECRET_KEY 示例：export SECRET_KEY='MyP@ssw0rd!'\n"
        "  该值须与 App 端调用 POST /key/register 时传入的 secret_key 完全一致。"
    )


# 启动时解析一次，后续复用
AES_KEY_BYTES: bytes = _resolve_aes_key()


# ─── AES-256-CBC 加解密 ───────────────────────────────────────

def aes_encrypt(plaintext: str) -> dict:
    """
    AES-256-CBC 加密，使用全局 AES_KEY_BYTES
    返回：{"iv": base64, "data": base64}
    """
    iv      = os.urandom(16)
    padder  = padding.PKCS7(128).padder()
    padded  = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    cipher  = Cipher(algorithms.AES(AES_KEY_BYTES), modes.CBC(iv), backend=default_backend())
    enc     = cipher.encryptor()
    ct      = enc.update(padded) + enc.finalize()
    return {
        "iv":   base64.b64encode(iv).decode(),
        "data": base64.b64encode(ct).decode(),
    }


def aes_decrypt(payload: dict) -> str:
    """
    AES-256-CBC 解密，使用全局 AES_KEY_BYTES
    payload: {"iv": base64, "data": base64}
    """
    iv        = base64.b64decode(payload["iv"])
    ct        = base64.b64decode(payload["data"])
    cipher    = Cipher(algorithms.AES(AES_KEY_BYTES), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    padded    = decryptor.update(ct) + decryptor.finalize()
    unpadder  = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


# ─── 命令执行 ─────────────────────────────────────────────────

_cwd      = os.path.expanduser("~")
_cwd_lock = threading.Lock()


def _is_blocked(command: str) -> bool:
    low = command.lower()
    return any(kw in low for kw in BLOCKED_KEYWORDS)


def execute_command(command: str, timeout: int = 30) -> dict:
    """
    在本地执行 shell 命令，返回结构化结果。
    支持跨命令持久化工作目录（cd 命令特殊处理）。
    """
    global _cwd
    start = datetime.datetime.now()

    # ── 特殊处理 cd 命令 ──────────────────────────────────────
    stripped = command.strip()
    is_cd = (
        stripped in ("cd", "cd ~")
        or (
            stripped.startswith("cd ")
            and ";" not in stripped
            and "|" not in stripped
            and "&&" not in stripped
        )
    )
    if is_cd:
        if stripped in ("cd", "cd ~"):
            target = os.path.expanduser("~")
        else:
            target = stripped[3:].strip().strip('"').strip("'")

        expanded = os.path.expanduser(target)
        with _cwd_lock:
            if not os.path.isabs(expanded):
                expanded = os.path.normpath(os.path.join(_cwd, expanded))

        if os.path.isdir(expanded):
            with _cwd_lock:
                _cwd = expanded
            return {
                "stdout":      f"cwd: {_cwd}",
                "stderr":      "",
                "returncode":  0,
                "duration_ms": 0,
                "cwd":         _cwd,
            }
        return {
            "stdout":      "",
            "stderr":      f"cd: no such directory: {expanded}",
            "returncode":  1,
            "duration_ms": 0,
            "cwd":         _cwd,
        }

    # ── 普通命令 ──────────────────────────────────────────────
    with _cwd_lock:
        safe_cwd    = _cwd.replace("'", "'\\''")
        current_cwd = _cwd

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
            "cwd":         current_cwd,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout":      "",
            "stderr":      f"命令超时（>{timeout}s）",
            "returncode":  124,
            "duration_ms": timeout * 1000,
            "cwd":         current_cwd,
        }
    except Exception as e:
        return {
            "stdout":      "",
            "stderr":      str(e),
            "returncode":  1,
            "duration_ms": 0,
            "cwd":         current_cwd,
        }


# ─── MQ 消息处理 ──────────────────────────────────────────────

def on_message(channel, method, properties, body, connection):
    """收到 MQ 消息的回调"""
    try:
        # 1. 解析外层 JSON（加密载体）
        envelope = json.loads(body.decode("utf-8"))
        log.info("收到消息，开始 AES 解密...")

        # 2. AES 解密
        try:
            plaintext = aes_decrypt(envelope)
        except Exception as e:
            log.error(f"AES 解密失败：{e}（请确认 SECRET_KEY 与 Producer 端一致）")
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        # 3. 解析命令消息
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

        log.info(
            f"[{cmd_id}] 执行命令：{command!r}"
            f"  timeout={timeout}s  reply_to={reply_to}"
        )

        # 4. 安全黑名单检查
        if _is_blocked(command):
            result = {
                "cmd_id":      cmd_id,
                "stdout":      "",
                "stderr":      "命令被安全策略拒绝（黑名单）",
                "returncode":  403,
                "duration_ms": 0,
            }
            log.warning(f"[{cmd_id}] 命令被黑名单拦截：{command!r}")

        elif not command:
            result = {
                "cmd_id":      cmd_id,
                "stdout":      "",
                "stderr":      "命令为空",
                "returncode":  1,
                "duration_ms": 0,
            }
            log.warning(f"[{cmd_id}] 收到空命令，跳过")

        else:
            # 5. 执行命令
            exec_result = execute_command(command, timeout)
            result      = {"cmd_id": cmd_id, **exec_result}
            log.info(
                f"[{cmd_id}] 执行完成"
                f"  rc={result.get('returncode')}"
                f"  duration={result.get('duration_ms')}ms"
                f"  cwd={result.get('cwd')}"
            )

        # 6. 加密结果
        try:
            encrypted_result = aes_encrypt(json.dumps(result, ensure_ascii=False))
        except Exception as e:
            log.error(f"[{cmd_id}] 结果加密失败：{e}")
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        # 7. 回写加密结果到 reply_to queue
        if reply_to:
            try:
                channel.queue_declare(queue=reply_to, durable=True)
                channel.basic_publish(
                    exchange="",
                    routing_key=reply_to,
                    body=json.dumps(encrypted_result, ensure_ascii=False).encode("utf-8"),
                    properties=pika.BasicProperties(
                        delivery_mode=2,
                        correlation_id=cmd_id,
                        content_type="application/json",
                    ),
                )
                log.info(f"[{cmd_id}] 加密结果已回写到 {reply_to}")
            except Exception as e:
                log.error(f"[{cmd_id}] 回写结果失败：{e}")

        # 8. ACK
        channel.basic_ack(delivery_tag=method.delivery_tag)

    except Exception as e:
        log.error(f"消息处理异常：{e}", exc_info=True)
        # NACK，requeue=False 避免毒消息死循环
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


# ─── RabbitMQ 连接与消费（含自动重连）──────────────────────────

def connect() -> pika.BlockingConnection:
    credentials = pika.PlainCredentials(MQ_USER, MQ_PASS)
    params      = pika.ConnectionParameters(
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
            log.info(f"连接 RabbitMQ {MQ_HOST}:{MQ_PORT}  vhost={MQ_VHOST}...")
            conn    = connect()
            channel = conn.channel()

            # 声明队列（幂等，不存在则创建）
            channel.queue_declare(queue=QUEUE_CMD,    durable=True)
            channel.queue_declare(queue=QUEUE_RESULT, durable=True)

            # 每次只取一条消息，处理完再取下一条（公平分发）
            channel.basic_qos(prefetch_count=1)

            channel.basic_consume(
                queue=QUEUE_CMD,
                on_message_callback=lambda ch, method, props, body: on_message(
                    ch, method, props, body, conn
                ),
            )

            log.info(f"✅  开始监听队列：{QUEUE_CMD}")
            log.info(f"    结果回写队列：{QUEUE_RESULT}")
            retry_delay = 5   # 重置退避时间
            channel.start_consuming()

        except pika.exceptions.AMQPConnectionError as e:
            log.error(f"RabbitMQ 连接断开：{e}，{retry_delay}s 后重试...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)

        except KeyboardInterrupt:
            log.info("收到中断信号，停止消费")
            break

        except Exception as e:
            log.error(f"未知错误：{e}，{retry_delay}s 后重试...", exc_info=True)
            time.sleep(retry_delay)


# ─── 工具：生成随机 AES-256 密钥 ──────────────────────────────

def generate_random_key():
    key = os.urandom(32)
    b64 = base64.b64encode(key).decode()
    print("新生成的随机 AES-256 密钥（base64）：")
    print(b64)
    print(f'\nexport AES_KEY="{b64}"')
    print("\n推荐做法：改用 SECRET_KEY + /key/register 接口，密钥由程序自动派生。")


def show_derived_key(secret: str):
    """打印由 secret 派生的 AES key（用于调试，确认两端一致）"""
    key = derive_aes_key(secret)
    b64 = base64.b64encode(key).decode()
    print(f"SECRET_KEY  : {secret}")
    print(f"MD5(secret) : {hashlib.md5(secret.encode()).hexdigest()}")
    print(f"AES key     : {b64}")
    print(f"长度        : {len(key)} bytes（AES-256）")


# ─── 入口 ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if "--gen-key" in sys.argv:
        generate_random_key()
        sys.exit(0)

    if "--show-derived-key" in sys.argv:
        idx = sys.argv.index("--show-derived-key")
        if idx + 1 < len(sys.argv):
            show_derived_key(sys.argv[idx + 1])
        else:
            print("用法：python3 mq_consumer.py --show-derived-key <secret_key>")
        sys.exit(0)

    # ── 启动前校验 ──────────────────────────────────────────
    if not USER_ID:
        log.error("环境变量 MQ_USER_ID 未设置！")
        log.error("  示例：export MQ_USER_ID=user123")
        sys.exit(1)

    if not SECRET_KEY and not AES_KEY_B64:
        log.error("未设置密钥环境变量！")
        log.error("  推荐：export SECRET_KEY='与 App 端 /key/register 一致的密钥'")
        log.error("  兼容：export AES_KEY='base64编码的32字节密钥'")
        log.error("  生成随机 key：python3 mq_consumer.py --gen-key")
        sys.exit(1)

    log.info("═" * 52)
    log.info("Shell Agent MQ Consumer 启动")
    log.info(f"  用户ID   : {USER_ID}")
    log.info(f"  监听队列 : {QUEUE_CMD}")
    log.info(f"  结果队列 : {QUEUE_RESULT}")
    log.info(f"  RabbitMQ : {MQ_HOST}:{MQ_PORT}  vhost={MQ_VHOST}")
    log.info(f"  密钥模式 : {'SECRET_KEY 本地派生' if SECRET_KEY else 'AES_KEY 直接使用（兼容模式）'}")
    log.info("═" * 52)

    start_consumer()
