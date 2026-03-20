"""
RabbitMQ 客户端
线程安全，自动重连，支持发布和消费
"""
import json
import threading
from typing import Callable, Optional

import pika
import pika.exceptions

from core.config import MQSettings
from core.exceptions import MQConnectionError, MQPublishError
from core.logging import get_logger

log = get_logger(__name__)


class MQClient:
    """
    RabbitMQ 线程安全客户端
    - 单连接复用，自动重连
    - 发布失败最多重试 3 次
    """

    def __init__(self, settings: MQSettings):
        self._settings = settings
        self._conn: Optional[pika.BlockingConnection] = None
        self._lock = threading.Lock()

    def _new_connection(self) -> pika.BlockingConnection:
        creds  = pika.PlainCredentials(self._settings.user, self._settings.password)
        params = pika.ConnectionParameters(
            host=self._settings.host,
            port=self._settings.port,
            virtual_host=self._settings.vhost,
            credentials=creds,
            heartbeat=self._settings.heartbeat,
            blocked_connection_timeout=30,
        )
        try:
            conn = pika.BlockingConnection(params)
            log.info(f"RabbitMQ 连接成功 {self._settings.host}:{self._settings.port}")
            return conn
        except Exception as e:
            raise MQConnectionError(str(e))

    def get_channel(self) -> pika.adapters.blocking_connection.BlockingChannel:
        with self._lock:
            if self._conn is None or self._conn.is_closed:
                self._conn = self._new_connection()
            return self._conn.channel()

    def ensure_queue(self, queue: str) -> None:
        """声明队列（幂等）"""
        ch = self.get_channel()
        ch.queue_declare(queue=queue, durable=True)

    def publish(
        self,
        queue:      str,
        body:       dict,
        cmd_id:     str,
        reply_to:   str,
        retries:    int = 3,
    ) -> None:
        """
        发布加密消息到队列

        Args:
            queue:    目标队列名
            body:     消息体 dict（已加密）
            cmd_id:   命令 ID（写入 correlation_id）
            reply_to: 结果回写队列
            retries:  失败重试次数
        """
        for attempt in range(1, retries + 1):
            try:
                ch = self.get_channel()
                self.ensure_queue(queue)
                self.ensure_queue(reply_to)
                ch.basic_publish(
                    exchange="",
                    routing_key=queue,
                    body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    properties=pika.BasicProperties(
                        delivery_mode=2,           # 持久化
                        correlation_id=cmd_id,
                        reply_to=reply_to,
                        content_type="application/json",
                    ),
                )
                log.debug(f"消息发布成功 queue={queue} cmd_id={cmd_id}")
                return
            except pika.exceptions.AMQPConnectionError as e:
                log.warning(f"发布失败（第{attempt}次），重置连接：{e}")
                self._conn = None
                if attempt == retries:
                    raise MQPublishError(str(e))
            except Exception as e:
                if attempt == retries:
                    raise MQPublishError(str(e))
                log.warning(f"发布失败（第{attempt}次）：{e}")

    def consume(
        self,
        queue:    str,
        callback: Callable,
        prefetch: int = 1,
    ) -> None:
        """
        阻塞消费队列（含自动重连）

        Args:
            queue:    消费的队列名
            callback: 消息回调 (channel, method, properties, body)
            prefetch: 每次预取消息数
        """
        import time
        retry_delay = 5
        while True:
            try:
                ch = self.get_channel()
                ch.queue_declare(queue=queue, durable=True)
                ch.basic_qos(prefetch_count=prefetch)
                ch.basic_consume(queue=queue, on_message_callback=callback)
                log.info(f"开始消费队列：{queue}")
                retry_delay = 5
                ch.start_consuming()
            except pika.exceptions.AMQPConnectionError as e:
                log.error(f"连接断开：{e}，{retry_delay}s 后重试")
                self._conn = None
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
            except KeyboardInterrupt:
                log.info("收到中断，停止消费")
                break

    def poll_result(
        self,
        reply_queue: str,
        cmd_id:      str,
        timeout:     int = 35,
    ) -> Optional[dict]:
        """
        轮询结果队列，返回匹配 cmd_id 的结果

        Returns:
            解密前的 dict，或 None（超时）
        """
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ch = self.get_channel()
                ch.queue_declare(queue=reply_queue, durable=True)
                method, props, body = ch.basic_get(queue=reply_queue, auto_ack=False)
                if method:
                    if props.correlation_id == cmd_id:
                        payload = json.loads(body.decode("utf-8"))
                        ch.basic_ack(delivery_tag=method.delivery_tag)
                        return payload
                    else:
                        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            except Exception as e:
                log.warning(f"轮询结果异常：{e}")
            time.sleep(0.5)
        return None

    def close(self) -> None:
        with self._lock:
            if self._conn and not self._conn.is_closed:
                self._conn.close()
                log.info("RabbitMQ 连接已关闭")
