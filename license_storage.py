"""Recoverable administrator display; client verification still uses hashes."""

import hashlib
import hmac
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

DISPLAY_KEY_PATH = Path(
    os.getenv(
        "LICENSE_DISPLAY_KEY_PATH",
        str(
            Path(os.getenv("DATA_DIR", str(Path(__file__).parent / "data")))
            / "license_display.key"
        ),
    )
)
_cipher = None


def ensure_display_key(*, existing_encrypted=False):
    global _cipher
    if not DISPLAY_KEY_PATH.exists():
        if existing_encrypted:
            raise RuntimeError(
                "卡密展示密钥缺失，请恢复原 license_display.key，禁止重新生成"
            )
        DISPLAY_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation never overwrites a key belonging to an existing DB.
        try:
            fd = os.open(DISPLAY_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as output:
                output.write(Fernet.generate_key())
                output.flush()
                os.fsync(output.fileno())
    try:
        _cipher = Fernet(DISPLAY_KEY_PATH.read_bytes().strip())
    except (OSError, ValueError) as exc:
        raise RuntimeError("卡密展示密钥无法读取，请恢复原密钥文件") from exc


def encrypt_license(value):
    if _cipher is None:
        raise RuntimeError("卡密展示密钥尚未初始化")
    return _cipher.encrypt(value.encode("utf-8")).decode("ascii")


def display_license(license):
    if license.key_hash is None:
        return license.key
    if not license.encrypted_key:
        return None
    if _cipher is None:
        raise RuntimeError("卡密展示密钥尚未初始化")
    try:
        value = _cipher.decrypt(license.encrypted_key.encode("ascii")).decode("utf-8")
        valid = value.split(".", 1)[0] == license.key and hmac.compare_digest(
            hashlib.sha256(value.encode()).hexdigest(), license.key_hash
        )
        if not valid:
            raise ValueError("mismatched record")
        return value
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise RuntimeError("卡密展示数据无法解密，请检查展示密钥和备份") from exc
