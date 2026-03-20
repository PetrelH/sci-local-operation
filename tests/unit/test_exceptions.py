"""异常体系单元测试"""
import pytest
from core.exceptions import (
    AppError, AuthError, CommandBlockedError,
    CommandTimeoutError, TaskNotFoundError, TaskStateError,
    MQPublishError, InvalidKeyError,
)


def test_auth_error_defaults():
    err = AuthError()
    assert err.status  == 401
    assert err.code    == "AUTH_ERROR"
    assert "认证失败" in err.message


def test_command_blocked_error():
    err = CommandBlockedError("rm -rf /")
    assert err.status == 403
    assert err.detail == "rm -rf /"
    d = err.to_dict()
    assert d["code"] == "COMMAND_BLOCKED"


def test_task_not_found():
    err = TaskNotFoundError("uuid-123")
    assert err.status == 404
    assert "uuid-123" in err.message


def test_task_state_error():
    err = TaskStateError("sent", ["pending", "failed"])
    assert err.status == 400
    assert "sent" in err.message


def test_mq_publish_error():
    err = MQPublishError("connection refused")
    assert err.status  == 502
    assert err.detail  == "connection refused"


def test_to_dict_structure():
    err = CommandTimeoutError(30)
    d   = err.to_dict()
    assert set(d.keys()) == {"code", "message", "detail"}
