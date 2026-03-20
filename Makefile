# ══════════════════════════════════════════════════════════════
# Shell Agent — Makefile
# 用法：make <target>
# ══════════════════════════════════════════════════════════════

.PHONY: help install dev-agent dev-producer dev-consumer \
        test test-unit test-cov lint fmt \
        docker-up docker-down docker-logs \
        db-init gen-key clean

# 默认目标
help:
	@echo ""
	@echo "  Shell Agent — 可用命令"
	@echo "  ─────────────────────────────────────────"
	@echo "  install          安装 Python 依赖"
	@echo "  dev-agent        启动 Agent 服务（开发模式）"
	@echo "  dev-producer     启动 Producer API（开发模式）"
	@echo "  dev-consumer     启动 MQ Consumer"
	@echo ""
	@echo "  test             运行所有测试"
	@echo "  test-unit        仅运行单元测试"
	@echo "  test-cov         测试 + 覆盖率报告"
	@echo "  lint             代码检查（ruff）"
	@echo "  fmt              代码格式化（ruff format）"
	@echo ""
	@echo "  docker-up        启动所有服务（Docker Compose）"
	@echo "  docker-down      停止所有服务"
	@echo "  docker-logs      查看服务日志"
	@echo ""
	@echo "  db-init          初始化数据库表"
	@echo "  gen-key          生成 AES-256 密钥"
	@echo "  clean            清理临时文件"
	@echo ""

# ── 安装 ──────────────────────────────────────────────────────

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements.txt
	pip install ruff

# ── 本地开发启动 ───────────────────────────────────────────────

dev-agent:
	@echo "启动 Agent 服务 → http://localhost:8000/docs"
	PYTHONPATH=. python apps/agent/main.py

dev-producer:
	@echo "启动 Producer API → http://localhost:9000/docs"
	PYTHONPATH=. python apps/producer/main.py

dev-consumer:
	@echo "启动 MQ Consumer (user: $$MQ_USER_ID)"
	PYTHONPATH=. python apps/consumer/main.py

# ── 测试 ──────────────────────────────────────────────────────

test:
	PYTHONPATH=. pytest tests/

test-unit:
	PYTHONPATH=. pytest tests/unit/ -v

test-cov:
	PYTHONPATH=. pytest tests/ --cov=. --cov-report=html --cov-report=term
	@echo "覆盖率报告：htmlcov/index.html"

# ── 代码质量 ──────────────────────────────────────────────────

lint:
	ruff check .

fmt:
	ruff format .

# ── Docker ───────────────────────────────────────────────────

docker-up:
	docker compose -f deploy/docker-compose.yml up -d
	@echo "服务已启动："
	@echo "  Agent:    http://localhost:8000/docs"
	@echo "  Producer: http://localhost:9000/docs"
	@echo "  RabbitMQ: http://localhost:15672 (guest/guest)"

docker-down:
	docker compose -f deploy/docker-compose.yml down

docker-logs:
	docker compose -f deploy/docker-compose.yml logs -f

docker-build:
	docker compose -f deploy/docker-compose.yml build

# ── 数据库 & 工具 ─────────────────────────────────────────────

db-init:
	PYTHONPATH=. python apps/producer/main.py --init-db

gen-key:
	@python3 -c "import os,base64; k=base64.b64encode(os.urandom(32)).decode(); print(f'AES_KEY={k}')"

# ── 清理 ──────────────────────────────────────────────────────

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "htmlcov" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	find . -name ".coverage" -delete 2>/dev/null || true
	@echo "清理完成"
