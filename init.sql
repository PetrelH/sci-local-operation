-- Shell Agent 命令任务表
-- 执行方式：mysql -u root -p shellagent < init.sql

CREATE DATABASE IF NOT EXISTS shellagent
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE shellagent;

CREATE TABLE IF NOT EXISTS command_tasks (
    -- 主键
    id              VARCHAR(36)     NOT NULL                    COMMENT '任务ID（UUID）',

    -- 目标信息
    user_id         VARCHAR(64)     NOT NULL                    COMMENT '目标用户ID，决定发到 agent.{user_id} 队列',
    command         TEXT            NOT NULL                    COMMENT '待执行的 Shell 命令',
    timeout         INT             NOT NULL DEFAULT 30         COMMENT '命令超时秒数',
    reply_to        VARCHAR(128)    NULL                        COMMENT '结果回写队列，NULL 则用 result.{user_id}',

    -- 状态流转
    --   pending  → 待发送（初始状态）
    --   sent     → 已加密发送到 MQ
    --   failed   → 超过最大重试次数，发送失败
    status          ENUM('pending','sent','failed')
                                    NOT NULL DEFAULT 'pending'  COMMENT '任务状态',

    -- MQ 追踪
    cmd_id          VARCHAR(36)     NULL                        COMMENT 'MQ 消息 ID，发送成功后填入',

    -- 重试控制
    retry_count     INT             NOT NULL DEFAULT 0          COMMENT '已重试次数',
    max_retries     INT             NOT NULL DEFAULT 3          COMMENT '最大重试次数，超过则标记 failed',
    error_msg       TEXT            NULL                        COMMENT '最近一次失败原因',

    -- 时间
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP  COMMENT '创建时间',
    sent_at         DATETIME        NULL                                 COMMENT '实际发送时间',
    scheduled_at    DATETIME        NULL                                 COMMENT '计划执行时间，NULL=立即执行',

    PRIMARY KEY (id),
    INDEX idx_status        (status),
    INDEX idx_user_id       (user_id),
    INDEX idx_scheduled_at  (scheduled_at),
    INDEX idx_created_at    (created_at)

) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='Shell Agent 命令任务表';
