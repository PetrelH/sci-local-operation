"""测试配置"""
import os
import pytest

# 测试环境强制使用测试配置
os.environ.setdefault("AGENT_TOKEN",        "test-token")
os.environ.setdefault("PRODUCER_API_TOKEN", "test-producer-token")
os.environ.setdefault("AES_KEY", "")   # 由各测试自行生成


@pytest.fixture(scope="session")
def aes_key():
    from core.crypto import generate_key
    return generate_key()
