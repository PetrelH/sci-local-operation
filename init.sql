-- Shell Agent 命令任务表
-- 执行方式：mysql -u root -p shellagent < init.sql

CREATE DATABASE IF NOT EXISTS shellagent
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE shellagent;


-- ══════════════════════════════════════════════════════════════
-- 命令任务表
-- ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS t_command_tasks (
    id              VARCHAR(36)     NOT NULL                    COMMENT '任务ID（UUID）',
    user_id         VARCHAR(64)     NOT NULL                    COMMENT '目标用户ID，决定发到 agent.{user_id} 队列',
    command         TEXT            NOT NULL                    COMMENT '待执行的 Shell 命令',
    timeout         INT             NOT NULL DEFAULT 30         COMMENT '命令超时秒数',
    reply_to        VARCHAR(128)    NULL                        COMMENT '结果回写队列，NULL 则用 result.{user_id}',
    `status`          ENUM('pending','sent','failed') NOT NULL DEFAULT 'pending'  COMMENT '任务状态',
    cmd_id          VARCHAR(36)     NULL                        COMMENT 'MQ 消息 ID，发送成功后填入',
    retry_count     INT             NOT NULL DEFAULT 0          COMMENT '已重试次数',
    max_retries     INT             NOT NULL DEFAULT 3          COMMENT '最大重试次数，超过则标记 failed',
    error_msg       TEXT            NULL                        COMMENT '最近一次失败原因',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP  COMMENT '创建时间',
    sent_at         DATETIME        NULL                                 COMMENT '实际发送时间',
    scheduled_at    DATETIME        NULL                                 COMMENT '计划执行时间，NULL=立即执行',
    PRIMARY KEY (`id`),
    INDEX idx_status        (`status`),
    INDEX idx_user_id       (`user_id`),
    INDEX idx_scheduled_at  (`scheduled_at`),
    INDEX idx_created_at    (`created_at`)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='Shell Agent 命令任务表';



-- ══════════════════════════════════════════════════════════════
-- 用户密钥表
-- 存储每个用户的原始 secret_key 及派生的 AES key
--
-- 密钥派生规则（Producer 与 Consumer 两端完全一致）：
--   step1 = MD5(secret_key)           → 16 bytes
--   step2 = MD5(step1)                → 16 bytes
--   AES key = step1 ‖ step2           → 32 bytes (AES-256)
--   aes_key_b64 = base64(AES key)
--
-- App 端调用 POST /key/register { user_id, secret_key }
-- Consumer 端设置环境变量 SECRET_KEY=<同一个 secret_key>，本地派生，AES key 不经过网络
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS t_user_keys (
    user_id         VARCHAR(64)     NOT NULL                    COMMENT '用户ID，与 command_tasks.user_id 关联',
    secret_key      VARCHAR(255)    NOT NULL                    COMMENT '原始密钥明文（App 端输入，仅服务端存储）',
    aes_key_b64     VARCHAR(255)    NOT NULL                    COMMENT '派生的 AES-256 key（base64），= MD5(secret) ‖ MD5(MD5(secret))',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP      COMMENT '首次注册时间',
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                    ON UPDATE CURRENT_TIMESTAMP             COMMENT '最后更新时间',

    PRIMARY KEY (user_id)

) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='用户密钥表：存储 secret_key 原文及派生 AES key';



-- ══════════════════════════════════════════════════════════════
-- 命令执行结果表
-- 存储从 RabbitMQ 接收到的命令执行结果（解密后）
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS t_command_results (
    id              VARCHAR(36)     NOT NULL                    COMMENT '结果ID（UUID）',
    cmd_id          VARCHAR(36)     NOT NULL                    COMMENT '命令ID，与 t_command_tasks.cmd_id 关联',
    user_id         VARCHAR(64)     NOT NULL                    COMMENT '用户ID',
    stdout          TEXT            NULL                        COMMENT '标准输出',
    stderr          TEXT            NULL                        COMMENT '标准错误',
    returncode      INT             NULL                        COMMENT '返回码',
    duration_ms     INT             NULL                        COMMENT '执行耗时（毫秒）',
    cwd             VARCHAR(512)    NULL                        COMMENT '执行时的工作目录',
    raw_result      TEXT            NULL                        COMMENT '原始结果JSON（保留完整信息）',
    received_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP  COMMENT '接收时间',

    PRIMARY KEY (id),
    UNIQUE INDEX idx_cmd_id     (cmd_id),
    INDEX idx_user_id           (user_id),
    INDEX idx_received_at       (received_at)

) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='命令执行结果表：存储从 RabbitMQ 接收的执行结果';
