"""
Shell Agent — MQ 发送端示例
加密命令并发送到指定用户的 agent.{user_id} 队列

依赖：
    pip install pika cryptography

用法：
    # 发送单条命令
    python3 mq_sender.py --user user123 --cmd "ls -la"

    # 指定超时和回调队列
    python3 mq_sender.py --user user123 --cmd "df -h" --timeout 10 --reply result.user123
"""

import os
import json
import base64
import uuid
import argparse
import pika
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend


# ─── AES-256-CBC 加解密（与 mq_consumer.py 完全一致）────────────────

def aes_encrypt(plaintext: str, key_b64: str) -> dict:
    key = base64.b64decode(key_b64)
    iv = os.urandom(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    ct = encryptor.update(padded) + encryptor.finalize()
    return {
        "iv":   base64.b64encode(iv).decode(),
        "data": base64.b64encode(ct).decode(),
    }


def aes_decrypt(payload: dict, key_b64: str) -> str:
    key = base64.b64decode(key_b64)
    iv  = base64.b64decode(payload["iv"])
    ct  = base64.b64decode(payload["data"])
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ct) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


# ─── 发送命令 ─────────────────────────────────────────────────

def send_command(
    user_id: str,
    command: str,
    aes_key: str,
    mq_host: str = "localhost",
    mq_port: int = 5672,
    mq_user: str = "guest",
    mq_pass: str = "guest",
    mq_vhost: str = "/",
    timeout: int = 30,
    reply_to: str = None,
    wait_result: bool = True,
) -> dict | None:
    """
    加密命令并发送到 agent.{user_id} 队列
    wait_result=True 时阻塞等待结果（最多 timeout+5 秒）
    """
    cmd_id   = str(uuid.uuid4())
    queue_cmd = f"agent.{user_id}"
    reply_queue = reply_to or f"result.{user_id}"

    # 构造消息
    msg = {
        "cmd_id":   cmd_id,
        "command":  command,
        "timeout":  timeout,
        "reply_to": reply_queue,
    }

    # AES 加密
    encrypted = aes_encrypt(json.dumps(msg, ensure_ascii=False), aes_key)

    # 连接 MQ
    credentials = pika.PlainCredentials(mq_user, mq_pass)
    params = pika.ConnectionParameters(
        host=mq_host, port=mq_port,
        virtual_host=mq_vhost,
        credentials=credentials,
        heartbeat=30,
    )
    conn = pika.BlockingConnection(params)
    channel = conn.channel()

    # 确保队列存在
    channel.queue_declare(queue=queue_cmd,   durable=True)
    channel.queue_declare(queue=reply_queue, durable=True)

    # 发送加密消息
    channel.basic_publish(
        exchange="",
        routing_key=queue_cmd,
        body=json.dumps(encrypted, ensure_ascii=False).encode("utf-8"),
        properties=pika.BasicProperties(
            delivery_mode=2,
            correlation_id=cmd_id,
            reply_to=reply_queue,
            content_type="application/json",
        ),
    )
    print(f"✉  已发送命令到 {queue_cmd}  cmd_id={cmd_id}")
    print(f"   命令：{command}")

    if not wait_result:
        conn.close()
        return None

    # 等待结果
    print(f"⏳  等待结果（queue={reply_queue}）...")
    result = None

    import time
    deadline = time.time() + timeout + 10

    while time.time() < deadline:
        method_frame, props, body = channel.basic_get(queue=reply_queue, auto_ack=False)
        if method_frame:
            # 检查 correlation_id 匹配
            if props.correlation_id == cmd_id:
                try:
                    encrypted_result = json.loads(body.decode("utf-8"))
                    plaintext = aes_decrypt(encrypted_result, aes_key)
                    result = json.loads(plaintext)
                    channel.basic_ack(delivery_tag=method_frame.delivery_tag)
                    break
                except Exception as e:
                    print(f"❌  解密结果失败：{e}")
                    channel.basic_nack(delivery_tag=method_frame.delivery_tag, requeue=False)
                    break
            else:
                # 不是自己的结果，放回去
                channel.basic_nack(delivery_tag=method_frame.delivery_tag, requeue=True)
        time.sleep(0.5)

    conn.close()

    if result:
        print(f"\n{'─'*40}")
        print(f"✅  结果（rc={result.get('returncode')}  {result.get('duration_ms', 0)}ms）")
        if result.get("stdout"):
            print(result["stdout"])
        if result.get("stderr"):
            print(f"[stderr] {result['stderr']}")
        print(f"{'─'*40}")
    else:
        print(f"⚠   等待超时，未收到结果")

    return result


# ─── CLI ─────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Shell Agent MQ 发送端")
    parser.add_argument("--user",    required=True,  help="目标用户ID")
    parser.add_argument("--cmd",     required=True,  help="要执行的 shell 命令")
    parser.add_argument("--key",     default=os.getenv("AES_KEY", ""), help="AES-256 密钥 base64")
    parser.add_argument("--host",    default=os.getenv("MQ_HOST", "10.17.1.17"))
    parser.add_argument("--port",    default=int(os.getenv("MQ_PORT", "5672")), type=int)
    parser.add_argument("--mq-user", default=os.getenv("MQ_USER", "guest"))
    parser.add_argument("--mq-pass", default=os.getenv("MQ_PASS", "guest"))
    parser.add_argument("--vhost",   default=os.getenv("MQ_VHOST", "/"))
    parser.add_argument("--timeout", default=30, type=int)
    parser.add_argument("--reply",   default=None, help="结果回写队列（默认 result.{user_id}）")
    parser.add_argument("--no-wait", action="store_true", help="不等待结果，fire and forget")
    args = parser.parse_args()

    if not args.key:
        print("❌  请设置 AES_KEY 环境变量或通过 --key 传入")
        exit(1)

    send_command(
        user_id=args.user,
        command=args.cmd,
        aes_key=args.key,
        mq_host=args.host,
        mq_port=args.port,
        mq_user=args.mq_user,
        mq_pass=args.mq_pass,
        mq_vhost=args.vhost,
        timeout=args.timeout,
        reply_to=args.reply,
        wait_result=not args.no_wait,
    )
