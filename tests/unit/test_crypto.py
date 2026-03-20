"""AES-256-CBC 加解密单元测试"""
import pytest
from core.crypto import encrypt, decrypt, generate_key
from core.exceptions import CryptoError, InvalidKeyError


@pytest.fixture
def valid_key() -> str:
    return generate_key()


def test_generate_key_length(valid_key):
    import base64
    assert len(base64.b64decode(valid_key)) == 32


def test_encrypt_decrypt_roundtrip(valid_key):
    plaintext = '{"cmd_id": "abc", "command": "ls -la", "timeout": 30}'
    payload   = encrypt(plaintext, valid_key)
    assert "iv"   in payload
    assert "data" in payload
    result = decrypt(payload, valid_key)
    assert result == plaintext


def test_encrypt_produces_different_iv(valid_key):
    plaintext = "test message"
    p1 = encrypt(plaintext, valid_key)
    p2 = encrypt(plaintext, valid_key)
    assert p1["iv"] != p2["iv"]   # 每次加密 IV 不同
    assert p1["data"] != p2["data"]


def test_decrypt_wrong_key(valid_key):
    plaintext = "hello world"
    payload   = encrypt(plaintext, valid_key)
    wrong_key = generate_key()
    with pytest.raises(CryptoError):
        decrypt(payload, wrong_key)


def test_invalid_key_empty():
    with pytest.raises(InvalidKeyError):
        encrypt("test", "")


def test_invalid_key_wrong_length():
    import base64
    short_key = base64.b64encode(b"short").decode()
    with pytest.raises(InvalidKeyError):
        encrypt("test", short_key)


def test_encrypt_unicode(valid_key):
    plaintext = '{"command": "echo 你好世界"}'
    payload   = encrypt(plaintext, valid_key)
    result    = decrypt(payload, valid_key)
    assert result == plaintext
