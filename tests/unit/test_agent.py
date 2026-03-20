"""Agent 服务单元测试"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    with patch("core.config.get_settings") as mock_settings:
        mock_settings.return_value.agent.token    = "test-token"
        mock_settings.return_value.agent.host     = "0.0.0.0"
        mock_settings.return_value.agent.port     = 8000
        mock_settings.return_value.agent.log_level = "DEBUG"
        from apps.agent.main import app
        return TestClient(app)


def test_health(client):
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"]  == "ok"
    assert "version" in data
    assert "cwd"     in data


def test_exec_requires_token(client):
    resp = client.post("/exec", json={"command": "echo hi"})
    assert resp.status_code == 422   # missing header


def test_exec_wrong_token(client):
    resp = client.post(
        "/exec",
        json={"command": "echo hi"},
        headers={"x-token": "wrong"},
    )
    assert resp.status_code == 401


def test_exec_blocked_command(client):
    resp = client.post(
        "/exec",
        json={"command": "rm -rf /"},
        headers={"x-token": "test-token"},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "COMMAND_BLOCKED"


def test_exec_echo(client):
    resp = client.post(
        "/exec",
        json={"command": "echo hello_test"},
        headers={"x-token": "test-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "hello_test" in data["stdout"]
    assert data["returncode"] == 0


def test_get_cwd(client):
    resp = client.get("/cwd", headers={"x-token": "test-token"})
    assert resp.status_code == 200
    assert "cwd" in resp.json()


def test_set_cwd_invalid(client):
    resp = client.post(
        "/cwd",
        json={"path": "/nonexistent/path/xyz"},
        headers={"x-token": "test-token"},
    )
    assert resp.status_code == 400
