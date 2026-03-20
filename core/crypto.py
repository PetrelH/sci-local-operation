"""
AES-256-CBC 加解密工具
兼容 Python 3.13，使用 cryptography 库
"""
import os
import base64
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend

from core.exceptions import CryptoError, InvalidKeyError


def _decode_key(key_b64: str) -> bytes:
    """解码并校验 AES 密钥"""
    if not key_b64:
        raise InvalidKeyError("AES_KEY 未设置")
    try:
        key = base64.b64decode(key_b64)
    except Exception as e:
        raise InvalidKeyError(f"base64 解码失败：{e}")
    if len(key) != 32:
        raise InvalidKeyError(f"须为 32 字节，当前 {len(key)} 字节")
    return key


def encrypt(plaintext: str, key_b64: str) -> dict:
    """
    AES-256-CBC 加密

    Args:
        plaintext: 明文字符串
        key_b64:   base64 编码的 32 字节密钥

    Returns:
        {"iv": "base64...", "data": "base64..."}
    """
    try:
        key    = _decode_key(key_b64)
        iv     = os.urandom(16)
        padder = padding.PKCS7(128).padder()
        padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        enc    = cipher.encryptor()
        ct     = enc.update(padded) + enc.finalize()
        return {
            "iv":   base64.b64encode(iv).decode(),
            "data": base64.b64encode(ct).decode(),
        }
    except (InvalidKeyError, CryptoError):
        raise
    except Exception as e:
        raise CryptoError(f"加密失败：{e}")


def decrypt(payload: dict, key_b64: str) -> str:
    """
    AES-256-CBC 解密

    Args:
        payload: {"iv": "base64...", "data": "base64..."}
        key_b64: base64 编码的 32 字节密钥

    Returns:
        明文字符串
    """
    try:
        key      = _decode_key(key_b64)
        iv       = base64.b64decode(payload["iv"])
        ct       = base64.b64decode(payload["data"])
        cipher   = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        dec      = cipher.decryptor()
        padded   = dec.update(ct) + dec.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
    except (InvalidKeyError, CryptoError):
        raise
    except Exception as e:
        raise CryptoError(f"解密失败：{e}")


def generate_key() -> str:
    """生成一个新的 AES-256 密钥，返回 base64 编码字符串"""
    return base64.b64encode(os.urandom(32)).decode()
